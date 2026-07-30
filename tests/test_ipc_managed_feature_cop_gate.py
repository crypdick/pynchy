"""Integration tests for Cop-gated managed-feature PR publication."""

from __future__ import annotations

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
