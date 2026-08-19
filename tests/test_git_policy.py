"""Tests for explicit worktree publication and direct PR creation helpers."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - test helpers mock subprocess behavior and exceptions
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlparse

from conftest import make_settings

from pynchy.host.container_manager.ipc.handlers_lifecycle import PublicationRepositoryError
from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.security.identity import ReceiptVerification
from pynchy.host.git_ops.api import (
    GIT_POLICY_MERGE,
    RepoContext,
    ensure_worktree,
    host_create_pr_from_worktree,
    resolve_git_policy,
)
from tests.git_policy_support import GitPolicyDeps, git

pytest_plugins = ("tests.git_policy_support",)


if TYPE_CHECKING:
    from pathlib import Path


def commit_feature(
    worktree: Path,
    *,
    content: str = "new feature",
    message: str = "add feature",
) -> None:
    """Commit one feature file in a temporary worktree."""
    (worktree / "feature.txt").write_text(content)
    git(worktree, "add", "feature.txt")
    git(worktree, "config", "user.email", "test@test.com")
    git(worktree, "config", "user.name", "Test")
    git(worktree, "commit", "-m", message)


class TestResolveGitPolicy:
    def test_default_is_merge_to_main(self):
        """Worktree sync has one config-driven policy: merge-to-main."""
        assert resolve_git_policy("nonexistent") == GIT_POLICY_MERGE


class TestHostCreatePrFromWorktree:
    def test_no_worktree(self, git_env: dict):
        """Returns error when worktree does not exist."""
        result = host_create_pr_from_worktree("nonexistent", git_env["repo_ctx"])
        assert result["success"] is False
        assert "No worktree found" in result["message"]

    def test_uncommitted_changes(self, git_env: dict):
        """Returns error when worktree has uncommitted changes."""
        worktree = ensure_worktree("agent-1", git_env["repo_ctx"]).path
        (worktree / "wip.txt").write_text("uncommitted")

        result = host_create_pr_from_worktree("agent-1", git_env["repo_ctx"])
        assert result["success"] is False
        assert "uncommitted changes" in result["message"]

    def test_nothing_to_push(self, git_env: dict):
        """Returns success when branch is already up to date."""
        ensure_worktree("agent-1", git_env["repo_ctx"])

        result = host_create_pr_from_worktree("agent-1", git_env["repo_ctx"])
        assert result["success"] is True
        assert "Already up to date" in result["message"]

    def test_push_success_and_pr_created(self, git_env: dict):
        """Commits are pushed and a PR opens."""
        worktree = ensure_worktree("agent-1", git_env["repo_ctx"]).path
        commit_feature(worktree)
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                return mock_run.results.pop(0)
            return real_run(args, **kwargs)

        mock_run.results = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
            ),
        ]
        with (
            patch("pynchy.host.git_ops.managed_feature.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch(
                "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
                return_value=str(git_env["origin"]),
            ),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_worktree("agent-1", git_env["repo_ctx"])

        assert result["success"] is True
        assert result["pr_url"] == "https://github.com/owner/repo/pull/1"
        assert "1 commit(s)" in result["message"]
        assert "PR" in result["message"]
        assert urlparse(result["message"].rpartition(" ")[2]).hostname == "github.com"
        assert "worktree/agent-1" in git(git_env["origin"], "branch").stdout

    def test_push_updates_existing_pr(self, git_env: dict):
        """An existing PR updates after branch push."""
        worktree = ensure_worktree("agent-1", git_env["repo_ctx"]).path
        commit_feature(worktree)
        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="https://github.com/owner/repo/pull/42\n",
                )
            return real_run(args, **kwargs)

        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_worktree("agent-1", git_env["repo_ctx"])

        assert result["success"] is True
        assert "PR updated" in result["message"]
        assert "pull/42" in result["message"]

    def test_push_uses_linear_branch_and_agent_pr_text(self, git_env: dict):
        """Publication can expose a stable branch without renaming its worktree."""
        worktree = ensure_worktree("agent-1", git_env["repo_ctx"]).path
        commit_feature(worktree)
        real_run = subprocess.run
        calls: list[list[str]] = []

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                calls.append(args)
                return mock_run.results.pop(0)
            return real_run(args, **kwargs)

        mock_run.results = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
            ),
        ]
        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_worktree(
                "agent-1",
                git_env["repo_ctx"],
                publication_branch="syn/247/fix-login",
                pr_title="Fix login",
                pr_body="## Summary\nFix the login flow.",
            )

        assert result["success"] is True
        assert "syn/247/fix-login" in git(git_env["origin"], "branch").stdout
        assert calls[1][calls[1].index("--head") + 1] == "syn/247/fix-login"
        assert calls[1][calls[1].index("--title") + 1] == "Fix login"
        assert calls[1][calls[1].index("--body") + 1] == "## Summary\nFix the login flow."

    def test_fetch_failure(self, git_env: dict):
        """Remote availability is checked before the publication decision."""
        worktree = ensure_worktree("agent-1", git_env["repo_ctx"]).path
        commit_feature(worktree)
        git(git_env["project"], "remote", "remove", "origin")

        result = host_create_pr_from_worktree("agent-1", git_env["repo_ctx"])
        assert result["success"] is False
        assert "git fetch failed" in result["message"]

    def test_recovered_branch_publishes_against_origin_not_host_main(self, git_env: dict):
        """A recovered worktree must not look empty when host main is ahead locally."""
        worktree = ensure_worktree("agent-1", git_env["repo_ctx"]).path
        git(worktree, "branch", "-m", "recovered/agent-1")
        commit_feature(worktree)
        git(git_env["project"], "merge", "--ff-only", "recovered/agent-1")

        real_run = subprocess.run

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                return mock_run.results.pop(0)
            return real_run(args, **kwargs)

        mock_run.results = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
            ),
        ]
        with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run):
            result = host_create_pr_from_worktree("agent-1", git_env["repo_ctx"])

        assert result["success"] is True
        assert "recovered/agent-1" in result["message"]
        assert "recovered/agent-1" in git(git_env["origin"], "branch").stdout

    def test_detached_worktree_does_not_publish(self, git_env: dict):
        """A detached worktree has no branch that can safely back a pull request."""
        worktree = ensure_worktree("agent-1", git_env["repo_ctx"]).path
        git(worktree, "checkout", "--detach")

        result = host_create_pr_from_worktree("agent-1", git_env["repo_ctx"])

        assert result["success"] is False
        assert "detached" in result["message"]
        assert "worktree/agent-1" not in git(git_env["origin"], "branch").stdout

    def test_push_failure_redacts_standalone_configured_token(self, git_env: dict):
        """A raw token in Git stderr never reaches IPC diagnostics."""
        worktree = ensure_worktree("agent-1", git_env["repo_ctx"]).path
        commit_feature(worktree)
        synthetic_token = "synthetic-sensitive-value"  # noqa: S105 - synthetic redaction fixture.  # pragma: allowlist secret
        real_run = subprocess.run

        def mock_git_process(args, **kwargs):
            if args[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=1,
                    stdout="",
                    stderr=f"remote rejected {synthetic_token} with HTTP 403",
                )
            return real_run(args, capture_output=True, text=True, check=False, **kwargs)

        with (
            patch(
                "pynchy.host.git_ops.worktree_sync.git_env_with_token",
                return_value={"GH_TOKEN": synthetic_token},
            ),
            patch(
                "pynchy.host.git_ops.utils._run_git_process",
                side_effect=mock_git_process,
            ),
        ):
            result = host_create_pr_from_worktree("agent-1", git_env["repo_ctx"])

        assert result["success"] is False
        assert synthetic_token not in result["message"]
        assert "***" in result["message"]
        assert "HTTP 403" in result["message"]

    def test_pr_creation_failure(self, git_env: dict):
        """PR creation failure still reports successful push."""
        worktree = ensure_worktree("agent-1", git_env["repo_ctx"]).path
        commit_feature(worktree)
        real_run = subprocess.run
        calls: list[list[str]] = []

        def mock_run(args, **kwargs):
            if args[0] == "gh":
                calls.append(args)
                return mock_run.results.pop(0)
            return real_run(args, **kwargs)

        mock_run.results = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="auth required"),
        ]
        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=mock_run),
        ):
            result = host_create_pr_from_worktree("agent-1", git_env["repo_ctx"])

        assert result["success"] is False
        assert "Pushed" in result["message"]
        assert "PR creation failed" in result["message"]
        assert [call[1:3] for call in calls] == [["pr", "list"], ["pr", "create"]]


class TestIpcPolicyRouting:
    """Tests that IPC handler publishes generic workspace worktrees."""

    async def test_cop_receives_the_committed_worktree_patch(
        self,
        git_policy_deps: GitPolicyDeps,
        git_env: dict,
        tmp_path: Path,
    ) -> None:
        repo_ctx = git_env["repo_ctx"]
        worktree = ensure_worktree("agent-1", repo_ctx).path
        commit_feature(
            worktree,
            content="review this committed change\n",
            message="add review fixture",
        )
        result_dir = tmp_path / "handler-data" / "ipc" / "agent-1" / "merge_results"
        result_dir.mkdir(parents=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "handler-data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree"
            ) as create_pr,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-cop-patch",
                    "publication": "pull-request",
                },
                "agent-1",
                False,
                git_policy_deps,
            )

        summary = cop.await_args.args[1]
        assert "Repository: owner/repo" in summary
        assert "diff --git a/feature.txt b/feature.txt" in summary
        assert "+review this committed change" in summary
        assert cop.await_args.kwargs["required_human_reason"] is None
        create_pr.assert_not_called()

    async def test_agent_publication_opens_pr_without_merging_or_deploying(
        self,
        git_policy_deps: GitPolicyDeps,
        tmp_path: Path,
    ) -> None:
        merge_results_dir = tmp_path / "data" / "ipc" / "agent-1" / "merge_results"
        merge_results_dir.mkdir(parents=True)
        fake_repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[fake_repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._publication_patch_context",
                return_value=("Committed patch:\n+safe change", None),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
                return_value={
                    "success": True,
                    "message": "Opened PR: https://github.com/owner/repo/pull/7",
                },
            ) as create_pr,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-pr",
                    "publication": "pull-request",
                },
                "agent-1",
                False,
                git_policy_deps,
            )

        create_pr.assert_called_once_with("agent-1", fake_repo_ctx)
        assert git_policy_deps.deploy_calls == []
        result = json.loads((merge_results_dir / "req-pr.json").read_text())
        assert "pull/7" in result["repos"]["owner/repo"]["message"]

    async def test_routed_conversation_publishes_its_own_worktree(
        self,
        git_policy_deps: GitPolicyDeps,
        tmp_path: Path,
    ) -> None:
        source_group = "agent-1__thread_conversation-conv_1"
        result_dir = tmp_path / "data" / "ipc" / source_group / "merge_results"
        result_dir.mkdir(parents=True)
        repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._publication_patch_context",
                return_value=("Committed patch:\\n+safe change", None),
            ) as patch_context,
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
                return_value={"success": True, "message": "Opened PR"},
            ) as create_pr,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-routed-pr",
                    "publication": "pull-request",
                },
                source_group,
                False,
                git_policy_deps,
            )

        patch_context.assert_called_once_with(source_group, [repo_ctx])
        create_pr.assert_called_once_with(source_group, repo_ctx)

    async def test_routed_host_publication_selects_only_its_source_repository(
        self,
        git_policy_deps: GitPolicyDeps,
        tmp_path: Path,
    ) -> None:
        source_group = "agent-1__thread_conversation-conv_1"
        result_dir = tmp_path / "data" / "ipc" / source_group / "merge_results"
        result_dir.mkdir(parents=True)
        source_repo = RepoContext(
            slug="owner/source", root=tmp_path / "source", worktrees_dir=tmp_path / "source-wt"
        )
        other_repo = RepoContext(
            slug="owner/other", root=tmp_path / "other", worktrees_dir=tmp_path / "other-wt"
        )

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._resolve_publication_repos",
                return_value=[source_repo],
            ) as resolve_publication_repos,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_current_turn",
                new_callable=AsyncMock,
                return_value=Mock(turn_id="turn-source"),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
                new_callable=AsyncMock,
                return_value=ReceiptVerification.VALID,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._publication_patch_context",
                side_effect=AssertionError("valid receipt must not inspect a second repository"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
                return_value={"success": True, "message": "Opened source PR"},
            ) as create_pr,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-routed-source-pr",
                    "publication": "pull-request",
                    "turn_id": "turn-source",
                },
                source_group,
                False,
                git_policy_deps,
            )

        resolve_publication_repos.assert_called_once_with(source_group, "turn-source")
        create_pr.assert_called_once_with(source_group, source_repo)
        result = json.loads((result_dir / "req-routed-source-pr.json").read_text())
        assert result["success"] is True
        assert set(result["repos"]) == {source_repo.slug}
        assert other_repo.slug not in result["repos"]

    async def test_stale_routed_host_publication_rejects_spoofed_repository_data(
        self,
        git_policy_deps: GitPolicyDeps,
        tmp_path: Path,
    ) -> None:
        source_group = "agent-1__thread_conversation-conv_1"
        result_dir = tmp_path / "data" / "ipc" / source_group / "merge_results"
        result_dir.mkdir(parents=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._resolve_publication_repos",
                side_effect=PublicationRepositoryError("Routed host turn is no longer active"),
            ) as resolve_publication_repos,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_current_turn",
                new_callable=AsyncMock,
                return_value=Mock(turn_id="turn-stale"),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
                new_callable=AsyncMock,
                return_value=ReceiptVerification.VALID,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree"
            ) as create_pr,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-stale-routed-host",
                    "publication": "pull-request",
                    "repo_access": "owner/agent-selected",
                    "groupFolder": "agent-1",
                    "turn_id": "turn-stale",
                },
                source_group,
                False,
                git_policy_deps,
            )

        resolve_publication_repos.assert_called_once_with(source_group, "turn-stale")
        create_pr.assert_not_called()
        result = json.loads((result_dir / "req-stale-routed-host.json").read_text())
        assert result == {
            "success": False,
            "message": "Publication blocked: Routed host turn is no longer active",
        }

    async def test_missing_routed_child_worktree_never_publishes_parent(
        self,
        git_policy_deps: GitPolicyDeps,
        tmp_path: Path,
    ) -> None:
        source_group = "agent-1__thread_conversation-conv_1"
        result_dir = tmp_path / "data" / "ipc" / source_group / "merge_results"
        result_dir.mkdir(parents=True)
        repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._publication_patch_context",
                return_value=(
                    f"Publish committed worktree from {source_group!r}.",
                    "Committed patch unavailable for owner/repo: worktree is missing",
                ),
            ) as patch_context,
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree"
            ) as create_pr,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-missing-routed-child",
                    "publication": "pull-request",
                },
                source_group,
                False,
                git_policy_deps,
            )

        patch_context.assert_called_once_with(source_group, [repo_ctx])
        create_pr.assert_not_called()

    async def test_publication_failure_diagnostic_is_redacted_and_bounded(
        self,
        git_policy_deps: GitPolicyDeps,
        tmp_path: Path,
    ) -> None:
        merge_results_dir = tmp_path / "data" / "ipc" / "agent-1" / "merge_results"
        merge_results_dir.mkdir(parents=True)
        fake_repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")
        unsafe_message = (
            "Push failed: https://credential-value@github.com/owner/repo returned HTTP 403 "
            + ("details " * 300)
        )

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[fake_repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._publication_patch_context",
                return_value=("Committed patch:\n+safe change", None),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
                return_value={"success": False, "message": unsafe_message},
            ),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-pr-failure",
                    "publication": "pull-request",
                },
                "agent-1",
                False,
                git_policy_deps,
            )

        result = json.loads((merge_results_dir / "req-pr-failure.json").read_text())
        diagnostic = result["repos"]["owner/repo"]["message"]
        assert "credential-value" not in diagnostic
        assert "https://***@github.com/owner/repo" in diagnostic
        assert "HTTP 403" in diagnostic
        assert len(diagnostic) <= 1000
