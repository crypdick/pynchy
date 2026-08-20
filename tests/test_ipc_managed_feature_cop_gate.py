"""Integration tests for Cop-gated managed-feature PR publication."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_settings

from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.security.identity import ReceiptVerification
from pynchy.host.git_ops.api import (
    ManagedFeaturePublication,
    ManagedFeatureResolution,
    RepoContext,
)


@pytest.fixture
def deps() -> NullIpcDeps:
    """Return host dependencies unused by the patched publication boundary."""
    return NullIpcDeps()


def _managed_publication(tmp_path, *, slug: str = "safe-feature") -> ManagedFeaturePublication:
    repo_ctx = RepoContext("owner/repo", tmp_path, tmp_path / "worktrees")
    return ManagedFeaturePublication(
        repo_ctx=repo_ctx,
        feature_slug=slug,
        worktree_path=tmp_path / ".worktrees" / slug,
        branch_name=slug,
        main_branch="main",
        remote_url="https://github.com/owner/repo.git",
        base_sha="b" * 40,
        head_sha="a" * 40,
        object_format="sha1",
        ahead=1,
        git_common_dir=tmp_path / ".git",
    )


class TestManagedFeatureCopGate:
    """Managed publication must bind Cop to one host-derived feature identity."""

    @pytest.mark.parametrize(
        ("patch_text", "reason"),
        [
            ("GIT binary patch", "Committed patch for owner/repo contains binary content"),
            ("patch" + "x" * 70_000, "Committed patch exceeds the Cop inspection context limit"),
        ],
        ids=["binary-patch", "oversized-patch"],
    )
    async def test_patch_content_requires_human_review(self, deps, tmp_path, patch_text, reason):
        publication = _managed_publication(tmp_path)
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[publication.repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.resolve_managed_feature_publication",
                return_value=ManagedFeatureResolution(publication, None),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.read_managed_feature_patch",
                return_value=(patch_text, None),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.host_create_pr_from_managed_feature"
            ) as publisher,
        ):
            await dispatch(
                {
                    "type": "publish_managed_feature",
                    "request_id": "managed-patch-review",
                    "publication": "pull-request",
                    "feature_slug": publication.feature_slug,
                },
                "admin-1",
                True,
                deps,
            )

        assert cop.await_args.kwargs["required_human_reason"] == reason
        publisher.assert_not_called()

    async def test_changed_feature_invalidates_a_stale_approval_binding(self, deps, tmp_path):
        publication = _managed_publication(tmp_path)
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[publication.repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.resolve_managed_feature_publication",
                return_value=ManagedFeatureResolution(publication, None),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
                new_callable=AsyncMock,
                return_value=ReceiptVerification.VALID,
            ) as receipt,
        ):
            await dispatch(
                {
                    "type": "publish_managed_feature",
                    "request_id": "managed-stale-binding",
                    "publication": "pull-request",
                    "feature_slug": publication.feature_slug,
                    "_approval_receipt": "receipt",
                    "_managed_feature_binding": {"feature_slug": "old-feature"},
                },
                "admin-1",
                True,
                deps,
            )

        receipt.assert_awaited_once()
        response = (
            tmp_path / "data" / "ipc" / "admin-1" / "merge_results" / "managed-stale-binding.json"
        )
        assert "managed feature changed after Cop inspection" in response.read_text()

    async def test_publication_rejects_a_group_without_a_repository(self, deps, tmp_path):
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch("pynchy.host.git_ops.repo.resolve_repos_for_group", return_value=[]),
        ):
            await dispatch(
                {
                    "type": "publish_managed_feature",
                    "request_id": "managed-no-repo",
                    "publication": "pull-request",
                    "feature_slug": "safe-feature",
                },
                "admin-1",
                True,
                deps,
            )

        result_dir = tmp_path / "data" / "ipc" / "admin-1" / "merge_results"
        response = result_dir / "managed-no-repo.json"
        assert "No repo configured for this group." in response.read_text()

    async def test_publication_reports_unresolved_feature(self, deps, tmp_path):
        publication = _managed_publication(tmp_path)
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[publication.repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.resolve_managed_feature_publication",
                return_value=ManagedFeatureResolution(None, "feature is not manifest-bound"),
            ),
        ):
            await dispatch(
                {
                    "type": "publish_managed_feature",
                    "request_id": "managed-unresolved",
                    "publication": "pull-request",
                    "feature_slug": publication.feature_slug,
                },
                "admin-1",
                True,
                deps,
            )

        response = (
            tmp_path / "data" / "ipc" / "admin-1" / "merge_results" / "managed-unresolved.json"
        )
        assert "feature is not manifest-bound" in response.read_text()

    async def test_cop_receives_bound_feature_and_publisher_revalidates(self, deps, tmp_path):
        publication = _managed_publication(tmp_path)
        result_dir = tmp_path / "data" / "ipc" / "admin-1" / "merge_results"
        result_dir.mkdir(parents=True)
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[publication.repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.resolve_managed_feature_publication",
                return_value=ManagedFeatureResolution(publication, None),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature._managed_feature_patch_context",
                return_value=("trusted managed patch", None),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=True,
            ) as cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.host_create_pr_from_managed_feature",
                return_value={"success": True, "message": "Opened PR: https://example.test/pull/1"},
            ) as publisher,
        ):
            await dispatch(
                {
                    "type": "publish_managed_feature",
                    "request_id": "managed-1",
                    "publication": "pull-request",
                    "feature_slug": publication.feature_slug,
                },
                "admin-1",
                True,
                deps,
            )

        assert cop.await_args.args[0] == "publish_managed_feature"
        assert cop.await_args.args[1] == "trusted managed patch"
        assert cop.await_args.kwargs["request_id"] == "managed-1"
        assert cop.await_args.args[2]["_managed_feature_binding"] == {
            "feature_slug": "safe-feature",
            "repository": "owner/repo",
            "branch": "safe-feature",
            "target_branch": "main",
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
        }
        publisher.assert_called_once_with(
            "safe-feature",
            [publication.repo_ctx],
            expected_binding={
                "feature_slug": "safe-feature",
                "repository": "owner/repo",
                "branch": "safe-feature",
                "target_branch": "main",
                "base_sha": "b" * 40,
                "head_sha": "a" * 40,
            },
        )
        assert "pull/1" in (result_dir / "managed-1.json").read_text()

    async def test_valid_receipt_publishes_without_redacting_non_text_result(self, deps, tmp_path):
        publication = _managed_publication(tmp_path)
        binding = {
            "feature_slug": publication.feature_slug,
            "repository": publication.repo_slug,
            "branch": publication.branch_name,
            "target_branch": publication.main_branch,
            "base_sha": publication.base_sha,
            "head_sha": publication.head_sha,
        }
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[publication.repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.resolve_managed_feature_publication",
                return_value=ManagedFeatureResolution(publication, None),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
                new_callable=AsyncMock,
                return_value=ReceiptVerification.VALID,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.host_create_pr_from_managed_feature",
                return_value={"success": True, "message": 42},
            ),
        ):
            await dispatch(
                {
                    "type": "publish_managed_feature",
                    "request_id": "managed-valid-receipt",
                    "publication": "pull-request",
                    "feature_slug": publication.feature_slug,
                    "_approval_receipt": "receipt",
                    "_managed_feature_binding": binding,
                },
                "admin-1",
                True,
                deps,
            )

        response = (
            tmp_path / "data" / "ipc" / "admin-1" / "merge_results" / "managed-valid-receipt.json"
        )
        assert '"message": 42' in response.read_text()

    async def test_cop_denial_and_invalid_receipt_cannot_publish(self, deps, tmp_path):
        publication = _managed_publication(tmp_path)
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[publication.repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.resolve_managed_feature_publication",
                return_value=ManagedFeatureResolution(publication, None),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature._managed_feature_patch_context",
                return_value=("trusted managed patch", None),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.host_create_pr_from_managed_feature"
            ) as publisher,
        ):
            await dispatch(
                {
                    "type": "publish_managed_feature",
                    "request_id": "managed-denied",
                    "publication": "pull-request",
                    "feature_slug": publication.feature_slug,
                },
                "admin-1",
                True,
                deps,
            )

        cop.assert_awaited_once()
        publisher.assert_not_called()

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[publication.repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.resolve_managed_feature_publication",
                return_value=ManagedFeatureResolution(publication, None),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
                new_callable=AsyncMock,
                return_value=ReceiptVerification.INVALID,
            ) as receipt,
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
            ) as cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.host_create_pr_from_managed_feature"
            ) as publisher,
        ):
            await dispatch(
                {
                    "type": "publish_managed_feature",
                    "request_id": "managed-invalid",
                    "publication": "pull-request",
                    "feature_slug": publication.feature_slug,
                    "_approval_receipt": "replayed",
                    "_managed_feature_binding": {
                        "feature_slug": publication.feature_slug,
                        "repository": publication.repo_slug,
                        "branch": publication.branch_name,
                        "target_branch": publication.main_branch,
                        "base_sha": publication.base_sha,
                        "head_sha": publication.head_sha,
                    },
                },
                "admin-1",
                True,
                deps,
            )

        receipt.assert_awaited_once()
        assert receipt.await_args.args[0] == "publish_managed_feature"
        cop.assert_not_awaited()
        publisher.assert_not_called()

    @pytest.mark.parametrize(
        "payload",
        [
            {"publication": "deploy", "feature_slug": "safe-feature"},
            {"publication": "pull-request"},
        ],
    )
    async def test_invalid_requests_cannot_reach_resolver_cop_or_publisher(
        self,
        deps,
        tmp_path,
        payload,
    ):
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.resolve_managed_feature_publication"
            ) as resolver,
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
            ) as cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.host_create_pr_from_managed_feature"
            ) as publisher,
        ):
            await dispatch(
                {"type": "publish_managed_feature", "request_id": "managed-invalid", **payload},
                "admin-1",
                True,
                deps,
            )

        resolver.assert_not_called()
        cop.assert_not_awaited()
        publisher.assert_not_called()

    @pytest.mark.action("lifecycle.managed.feature.rebase")
    async def test_rebase_uses_only_host_bound_slug_without_cop(self, deps, tmp_path):
        repo_ctx = RepoContext("owner/repo", tmp_path, tmp_path / "worktrees")
        result_dir = tmp_path / "data" / "ipc" / "agent-1" / "merge_results"
        result_dir.mkdir(parents=True)
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature._resolve_repos_for_group",
                return_value=[repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.host_rebase_managed_feature",
                return_value={"success": True, "message": "rebased"},
            ) as rebase,
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
            ) as cop,
        ):
            await dispatch(
                {
                    "type": "rebase_managed_feature",
                    "request_id": "managed-rebase",
                    "feature_slug": "safe-feature",
                },
                "agent-1",
                False,
                deps,
            )

        assert json.loads((result_dir / "managed-rebase.json").read_text()) == {
            "success": True,
            "message": "rebased",
        }
        rebase.assert_called_once_with("safe-feature", [repo_ctx])
        cop.assert_not_awaited()


@pytest.mark.action("lifecycle.managed.feature.rebase")
class TestManagedFeatureRebaseIpc:
    """The rebase IPC surface returns host results without publishing a PR."""

    async def test_rejects_a_missing_feature_slug(self, deps, tmp_path):
        with patch(
            "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
            return_value=make_settings(data_dir=tmp_path / "data"),
        ):
            await dispatch(
                {"type": "rebase_managed_feature", "request_id": "missing-slug"},
                "admin-1",
                True,
                deps,
            )

        response = tmp_path / "data" / "ipc" / "admin-1" / "merge_results" / "missing-slug.json"
        assert "feature_slug must be a non-empty string" in response.read_text()

    async def test_rejects_a_group_without_a_repository(self, deps, tmp_path):
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch("pynchy.host.git_ops.repo.resolve_repos_for_group", return_value=[]),
        ):
            await dispatch(
                {
                    "type": "rebase_managed_feature",
                    "request_id": "missing-repo",
                    "feature_slug": "safe-feature",
                },
                "admin-1",
                True,
                deps,
            )

        response = tmp_path / "data" / "ipc" / "admin-1" / "merge_results" / "missing-repo.json"
        assert "No repo configured for this group." in response.read_text()
