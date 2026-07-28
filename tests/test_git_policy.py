"""Tests for explicit worktree publication and direct PR creation helpers.

Covers:
- host_create_pr_from_worktree() behavior
- IPC sync handler routing
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - test helpers mock subprocess behavior and exceptions
from contextlib import ExitStack
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, init_test_database, make_settings

from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.git_ops.api import (
    GIT_POLICY_MERGE,
    RepoContext,
    ensure_worktree,
    host_create_pr_from_worktree,
    resolve_git_policy,
)
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test helper runs fixed git argv against temp repos
        ["git", *args],  # noqa: S607 - test helper deliberately resolves git from PATH
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_bare_origin(tmp_path: Path) -> Path:
    """Create a bare 'origin' repo with one commit on main."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")

    clone = tmp_path / "setup-clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@test.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "README.md").write_text("initial")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-m", "initial commit")
    _git(clone, "push", "origin", "main")
    return origin


def _make_project(tmp_path: Path, origin: Path) -> Path:
    """Clone origin into a 'project' directory."""
    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))
    _git(project, "config", "user.email", "test@test.com")
    _git(project, "config", "user.name", "Test")
    return project


@pytest.fixture
def git_env(tmp_path: Path):
    """Set up origin + project repos with patched settings."""
    origin = _make_bare_origin(tmp_path)
    project = _make_project(tmp_path, origin)
    worktrees_dir = tmp_path / "worktrees"

    s = make_settings(project_root=project, worktrees_dir=worktrees_dir)
    repo_ctx = RepoContext(slug="owner/repo", root=project, worktrees_dir=worktrees_dir)

    with ExitStack() as stack:
        stack.enter_context(patch("pynchy.host.git_ops.utils._default_cwd", s.project_root))
        yield {
            "origin": origin,
            "project": project,
            "worktrees_dir": worktrees_dir,
            "repo_ctx": repo_ctx,
            "settings": s,
        }


# ---------------------------------------------------------------------------
# resolve_git_policy tests
# ---------------------------------------------------------------------------


class TestResolveGitPolicy:
    def test_default_is_merge_to_main(self):
        """Worktree sync has one config-driven policy: merge-to-main."""
        assert resolve_git_policy("nonexistent") == GIT_POLICY_MERGE


# ---------------------------------------------------------------------------
# host_create_pr_from_worktree tests
# ---------------------------------------------------------------------------


class TestHostCreatePrFromWorktree:
    def test_no_worktree(self, git_env: dict):
        """Returns error when worktree doesn't exist."""
        repo_ctx = git_env["repo_ctx"]
        result = host_create_pr_from_worktree("nonexistent", repo_ctx)
        assert result["success"] is False
        assert "No worktree found" in result["message"]

    def test_uncommitted_changes(self, git_env: dict):
        """Returns error when worktree has uncommitted changes."""
        repo_ctx = git_env["repo_ctx"]
        wt_result = ensure_worktree("agent-1", repo_ctx)
        (wt_result.path / "wip.txt").write_text("uncommitted")

        result = host_create_pr_from_worktree("agent-1", repo_ctx)
        assert result["success"] is False
        assert "uncommitted changes" in result["message"]

    def test_nothing_to_push(self, git_env: dict):
        """Returns success when already up to date."""
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)

        result = host_create_pr_from_worktree("agent-1", repo_ctx)
        assert result["success"] is True
        assert "Already up to date" in result["message"]

    def test_push_success_and_pr_created(self, git_env: dict):
        """Commits are pushed and a PR is opened."""
        repo_ctx = git_env["repo_ctx"]
        wt_result = ensure_worktree("agent-1", repo_ctx)
        wt_path = wt_result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        # Mock only gh CLI calls — delegate git calls to real subprocess
        real_run = subprocess.run

        def _mock_run(args, **kwargs):
            if args[0] == "gh":
                # First gh call: pr view (no existing PR)
                # Second gh call: pr create (success)
                return _mock_run._next_gh_result.pop(0)
            return real_run(args, **kwargs)

        _mock_run._next_gh_result = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="https://github.com/owner/repo/pull/1\n"
            ),
        ]

        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=_mock_run),
        ):
            result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result["success"] is True
        assert "1 commit(s)" in result["message"]
        assert "PR" in result["message"]
        assert "https://github.com" in result["message"]

        # Verify branch was pushed to origin
        branches = _git(git_env["origin"], "branch")
        assert "worktree/agent-1" in branches.stdout

    def test_push_updates_existing_pr(self, git_env: dict):
        """When a PR already exists, just push (PR auto-updates)."""
        repo_ctx = git_env["repo_ctx"]
        wt_result = ensure_worktree("agent-1", repo_ctx)
        wt_path = wt_result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        real_run = subprocess.run

        def _mock_run(args, **kwargs):
            if args[0] == "gh":
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="https://github.com/owner/repo/pull/42\n",
                )
            return real_run(args, **kwargs)

        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=_mock_run),
        ):
            result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result["success"] is True
        assert "PR updated" in result["message"]
        assert "pull/42" in result["message"]

    def test_push_failure(self, git_env: dict):
        """Push failure returns an error."""
        repo_ctx = git_env["repo_ctx"]
        wt_result = ensure_worktree("agent-1", repo_ctx)
        wt_path = wt_result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        # Make push fail by removing the origin remote
        _git(git_env["project"], "remote", "remove", "origin")

        result = host_create_pr_from_worktree("agent-1", repo_ctx)
        assert result["success"] is False
        assert "Push failed" in result["message"]

    def test_push_failure_redacts_standalone_configured_token(self, git_env: dict):
        """A raw token in Git stderr is removed before reaching IPC diagnostics."""
        repo_ctx = git_env["repo_ctx"]
        wt_result = ensure_worktree("agent-1", repo_ctx)
        wt_path = wt_result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")
        synthetic_token = "synthetic-sensitive-value"  # noqa: S105 - synthetic redaction fixture.  # pragma: allowlist secret
        real_run = subprocess.run

        def _mock_git_process(args, **kwargs):
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
                "pynchy.host.git_ops.sync.git_env_with_token",
                return_value={"GH_TOKEN": synthetic_token},
            ),
            patch(
                "pynchy.host.git_ops.utils._run_git_process",
                side_effect=_mock_git_process,
            ),
        ):
            result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result["success"] is False
        assert synthetic_token not in result["message"]
        assert "***" in result["message"]
        assert "HTTP 403" in result["message"]

    def test_pr_creation_failure(self, git_env: dict):
        """PR creation failure still reports that push succeeded."""
        repo_ctx = git_env["repo_ctx"]
        wt_result = ensure_worktree("agent-1", repo_ctx)
        wt_path = wt_result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        real_run = subprocess.run

        def _mock_run(args, **kwargs):
            if args[0] == "gh":
                return _mock_run._next_gh_result.pop(0)
            return real_run(args, **kwargs)

        _mock_run._next_gh_result = [
            # gh pr view: no existing PR
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
            # gh pr create: failure
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="auth required"),
        ]

        with (
            patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None),
            patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=_mock_run),
        ):
            result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result["success"] is False
        assert "Pushed" in result["message"]
        assert "PR creation failed" in result["message"]


# ---------------------------------------------------------------------------
# IPC handler routing tests
# ---------------------------------------------------------------------------


class MockDeps(NullIpcDeps):
    """Mock IPC dependencies for handler tests."""

    def __init__(self, groups: dict[str, WorkspaceProfile]):
        self._groups = groups
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.deploy_calls: list[tuple[str, bool]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_chats: list[str] = []
        self.enqueued_checks: list[str] = []

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.host_messages.append((jid, text))

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        self.system_notices.append((jid, text))

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    async def clear_session(self, group_folder: str) -> None:
        self.cleared_sessions.append(group_folder)

    async def clear_chat_history(self, chat_jid: str) -> None:
        self.cleared_chats.append(chat_jid)

    def enqueue_message_check(self, group_jid: str) -> None:
        self.enqueued_checks.append(group_jid)

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None:
        self.deploy_calls.append((previous_sha, rebuild))


@pytest.fixture
async def deps():
    await init_test_database()
    return MockDeps(
        {
            "agent@g.us": WorkspaceProfile(
                jid="agent@g.us",
                name="Agent",
                folder="agent-1",
                trigger="@test",
                added_at="2024-01-01",
            ),
        }
    )


class TestIpcPolicyRouting:
    """Tests that the IPC handler syncs worktrees into main."""

    async def test_cop_receives_the_committed_worktree_patch(
        self,
        deps: MockDeps,
        git_env: dict,
        tmp_path: Path,
    ) -> None:
        repo_ctx = git_env["repo_ctx"]
        worktree = ensure_worktree("agent-1", repo_ctx).path
        (worktree / "feature.txt").write_text("review this committed change\n")
        _git(worktree, "add", "feature.txt")
        _git(worktree, "config", "user.email", "test@test.com")
        _git(worktree, "config", "user.name", "Test")
        _git(worktree, "commit", "-m", "add review fixture")
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
                deps,
            )

        summary = cop.await_args.args[1]
        assert "Repository: owner/repo" in summary
        assert "diff --git a/feature.txt b/feature.txt" in summary
        assert "+review this committed change" in summary
        assert cop.await_args.kwargs["required_human_reason"] is None
        create_pr.assert_not_called()

    async def test_agent_publication_opens_pr_without_merging_or_deploying(
        self,
        deps: MockDeps,
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
                deps,
            )

        create_pr.assert_called_once_with("agent-1", fake_repo_ctx)
        assert deps.deploy_calls == []
        result = json.loads((merge_results_dir / "req-pr.json").read_text())
        assert "pull/7" in result["repos"]["owner/repo"]["message"]

    async def test_publication_failure_diagnostic_is_redacted_and_bounded(
        self,
        deps: MockDeps,
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
                deps,
            )

        result = json.loads((merge_results_dir / "req-pr-failure.json").read_text())
        diagnostic = result["repos"]["owner/repo"]["message"]
        assert "credential-value" not in diagnostic
        assert "https://***@github.com/owner/repo" in diagnostic
        assert "HTTP 403" in diagnostic
        assert len(diagnostic) <= 1000
