"""Tests for managed-feature Cop inspection integrity."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from conftest import make_settings

from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.git_ops.api import (
    read_managed_feature_patch,
    resolve_managed_feature_publication,
)
from tests.git_policy_support import (
    GitPolicyDeps,
    create_managed_feature,
    git,
    managed_record,
    write_managed_manifest,
)

pytest_plugins = ("tests.git_policy_support",)

if TYPE_CHECKING:
    from pathlib import Path


class TestManagedFeatureCopPatch:
    """Cop inspects raw commits, never agent-owned replacement refs."""

    async def test_cop_ignores_agent_replace_ref(
        self, git_policy_deps: GitPolicyDeps, git_env: dict, tmp_path: Path
    ):
        worktree = create_managed_feature(git_env, "replace-feature")
        write_managed_manifest(
            git_env["project"],
            [managed_record("replace-feature")],
        )
        feature_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
        main_sha = git(git_env["project"], "rev-parse", "main").stdout.strip()
        git(git_env["project"], "replace", feature_sha, main_sha)
        result_dir = tmp_path / "handler-data" / "ipc" / "agent-1" / "merge_results"
        result_dir.mkdir(parents=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "handler-data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[git_env["repo_ctx"]],
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as cop,
        ):
            await dispatch(
                {
                    "type": "publish_managed_feature",
                    "request_id": "replace-ref",
                    "publication": "pull-request",
                    "feature_slug": "replace-feature",
                },
                "agent-1",
                False,
                git_policy_deps,
            )

        result = json.loads((result_dir / "replace-ref.json").read_text())
        assert cop.await_args is not None, result["message"]
        summary = cop.await_args.args[1]
        assert "+replace-feature change" in summary
        assert "(no committed diff)" not in summary

    async def test_oversized_patch_requires_human_review_without_cop_content(
        self, git_policy_deps: GitPolicyDeps, git_env: dict, tmp_path: Path
    ):
        """Cop receives no partial patch when committed output exceeds its limit."""
        worktree = create_managed_feature(git_env, "oversized-feature")
        (worktree / "oversized.txt").write_text("x" * (64 * 1024 + 1), encoding="utf-8")
        git(worktree, "add", "oversized.txt")
        git(worktree, "commit", "-m", "add oversized patch")
        write_managed_manifest(
            git_env["project"],
            [managed_record("oversized-feature")],
        )
        resolution = resolve_managed_feature_publication("oversized-feature", [git_env["repo_ctx"]])
        publication = resolution.publication
        assert publication is not None, resolution.error
        patch_text, diagnostic = read_managed_feature_patch(publication)
        assert patch_text is None
        assert diagnostic == "Committed patch exceeds the Cop inspection context limit"

        result_dir = tmp_path / "handler-data" / "ipc" / "agent-1" / "merge_results"
        result_dir.mkdir(parents=True)
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_managed_feature.get_settings",
                return_value=make_settings(data_dir=tmp_path / "handler-data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[git_env["repo_ctx"]],
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
                    "request_id": "oversized-patch",
                    "publication": "pull-request",
                    "feature_slug": "oversized-feature",
                },
                "agent-1",
                False,
                git_policy_deps,
            )

        assert cop.await_args is not None
        assert "x" * 100 not in cop.await_args.args[1]
        assert cop.await_args.kwargs["required_human_reason"] == (
            "Committed patch unavailable for owner/repo: "
            "Committed patch exceeds the Cop inspection context limit"
        )
        publisher.assert_not_called()
