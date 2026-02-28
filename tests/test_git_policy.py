"""Tests for workspace git policy: merge-to-main vs pull-request.

Covers:
- resolve_git_policy() resolution logic
- host_create_pr_from_worktree() behavior
- IPC handler routing based on policy
- merge_worktree_with_policy() and background_merge_worktree() dispatch
"""

from __future__ import annotations

import json
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.config import WorkspaceConfig
from pynchy.state import _init_test_database
from pynchy.host.git_ops.repo import RepoContext
from pynchy.host.git_ops.sync import (
    GIT_POLICY_MERGE,
    GIT_POLICY_PR,
    host_create_pr_from_worktree,
    resolve_git_policy,
)
from pynchy.host.container_manager.ipc import dispatch
from pynchy.types import WorkspaceProfile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
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
        stack.enter_context(patch("pynchy.host.git_ops.utils.get_settings", return_value=s))
        stack.enter_context(patch("pynchy.host.git_ops.sync.get_settings", return_value=s))
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
        """No git_policy configured → merge-to-main."""
        s = make_settings(workspaces={})
        with patch("pynchy.host.git_ops.sync.get_settings", return_value=s):
            assert resolve_git_policy("nonexistent") == GIT_POLICY_MERGE

    def test_none_resolves_to_merge(self):
        """git_policy=None → merge-to-main."""
        s = make_settings(
            workspaces={
                "agent-1": WorkspaceConfig(
                    name="Agent 1",
                    is_admin=False,
                    git_policy=None,
                ),
            }
        )
        with patch("pynchy.host.git_ops.sync.get_settings", return_value=s):
            assert resolve_git_policy("agent-1") == GIT_POLICY_MERGE

    def test_merge_to_main_explicit(self):
        """git_policy="merge-to-main" → merge-to-main."""
        s = make_settings(
            workspaces={
                "agent-1": WorkspaceConfig(
                    name="Agent 1",
                    is_admin=False,
                    git_policy="merge-to-main",
                ),
            }
        )
        with patch("pynchy.host.git_ops.sync.get_settings", return_value=s):
            assert resolve_git_policy("agent-1") == GIT_POLICY_MERGE

    def test_pull_request_policy(self):
        """git_policy="pull-request" → pull-request."""
        s = make_settings(
            workspaces={
                "experimental": WorkspaceConfig(
                    name="Experimental",
                    is_admin=False,
                    git_policy="pull-request",
                ),
            }
        )
        with patch("pynchy.host.git_ops.sync.get_settings", return_value=s):
            assert resolve_git_policy("experimental") == GIT_POLICY_PR


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
        from pynchy.host.git_ops.worktree import ensure_worktree

        repo_ctx = git_env["repo_ctx"]
        wt_result = ensure_worktree("agent-1", repo_ctx)
        (wt_result.path / "wip.txt").write_text("uncommitted")

        result = host_create_pr_from_worktree("agent-1", repo_ctx)
        assert result["success"] is False
        assert "uncommitted changes" in result["message"]

    def test_nothing_to_push(self, git_env: dict):
        """Returns success when already up to date."""
        from pynchy.host.git_ops.worktree import ensure_worktree

        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)

        result = host_create_pr_from_worktree("agent-1", repo_ctx)
        assert result["success"] is True
        assert "Already up to date" in result["message"]

    def test_push_success_and_pr_created(self, git_env: dict):
        """Commits are pushed and a PR is opened."""
        from pynchy.host.git_ops.worktree import ensure_worktree

        repo_ctx = git_env["repo_ctx"]
        wt_result = ensure_worktree("agent-1", repo_ctx)
        wt_path = wt_result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        # Mock only gh CLI calls — delegate git calls to real subprocess
        _real_run = subprocess.run

        def _mock_run(args, **kwargs):
            if args[0] == "gh":
                # First gh call: pr view (no existing PR)
                # Second gh call: pr create (success)
                return _mock_run._next_gh_result.pop(0)
            return _real_run(args, **kwargs)

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
        from pynchy.host.git_ops.worktree import ensure_worktree

        repo_ctx = git_env["repo_ctx"]
        wt_result = ensure_worktree("agent-1", repo_ctx)
        wt_path = wt_result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        _real_run = subprocess.run

        def _mock_run(args, **kwargs):
            if args[0] == "gh":
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="https://github.com/owner/repo/pull/42\n",
                )
            return _real_run(args, **kwargs)

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
        from pynchy.host.git_ops.worktree import ensure_worktree

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

    def test_pr_creation_failure(self, git_env: dict):
        """PR creation failure still reports that push succeeded."""
        from pynchy.host.git_ops.worktree import ensure_worktree

        repo_ctx = git_env["repo_ctx"]
        wt_result = ensure_worktree("agent-1", repo_ctx)
        wt_path = wt_result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        _real_run = subprocess.run

        def _mock_run(args, **kwargs):
            if args[0] == "gh":
                return _mock_run._next_gh_result.pop(0)
            return _real_run(args, **kwargs)

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


class MockDeps:
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

    async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
        self.deploy_calls.append((previous_sha, rebuild))


@pytest.fixture
async def deps():
    await _init_test_database()
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
    """Tests that the IPC handler routes to the correct function based on policy."""

    async def test_merge_policy_calls_host_sync(self, deps: MockDeps, tmp_path: Path):
        """merge-to-main policy calls host_sync_worktree."""
        merge_results_dir = tmp_path / "data" / "ipc" / "agent-1" / "merge_results"
        merge_results_dir.mkdir(parents=True)
        fake_repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.resolve_git_policy",
                return_value=GIT_POLICY_MERGE,
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repo_for_group",
                return_value=fake_repo_ctx,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_sync_worktree",
                return_value={"success": True, "message": "Merged 1 commit(s)"},
            ) as mock_sync,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
            ) as mock_pr,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_notify_worktree_updates",
                new_callable=AsyncMock,
            ),
        ):
            await dispatch(
                {"type": "sync_worktree_to_main", "requestId": "req-1"},
                "agent-1",
                False,
                deps,
            )

        mock_sync.assert_called_once()
        mock_pr.assert_not_called()

        result_file = merge_results_dir / "req-1.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text())
        assert data["success"] is True

    async def test_pr_policy_calls_host_create_pr(self, deps: MockDeps, tmp_path: Path):
        """pull-request policy calls host_create_pr_from_worktree."""
        merge_results_dir = tmp_path / "data" / "ipc" / "agent-1" / "merge_results"
        merge_results_dir.mkdir(parents=True)
        fake_repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.resolve_git_policy",
                return_value=GIT_POLICY_PR,
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repo_for_group",
                return_value=fake_repo_ctx,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_sync_worktree",
            ) as mock_sync,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
                return_value={"success": True, "message": "Pushed 1 commit(s) and opened PR"},
            ) as mock_pr,
        ):
            await dispatch(
                {"type": "sync_worktree_to_main", "requestId": "req-2"},
                "agent-1",
                False,
                deps,
            )

        mock_pr.assert_called_once()
        mock_sync.assert_not_called()

        result_file = merge_results_dir / "req-2.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text())
        assert data["success"] is True

    async def test_pr_policy_skips_worktree_notifications(self, deps: MockDeps, tmp_path: Path):
        """PR policy doesn't notify other worktrees (main didn't change)."""
        merge_results_dir = tmp_path / "data" / "ipc" / "agent-1" / "merge_results"
        merge_results_dir.mkdir(parents=True)
        fake_repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.resolve_git_policy",
                return_value=GIT_POLICY_PR,
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repo_for_group",
                return_value=fake_repo_ctx,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
                return_value={"success": True, "message": "Pushed"},
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_notify_worktree_updates",
                new_callable=AsyncMock,
            ) as mock_notify,
        ):
            await dispatch(
                {"type": "sync_worktree_to_main", "requestId": "req-3"},
                "agent-1",
                False,
                deps,
            )

        mock_notify.assert_not_called()

    async def test_pr_policy_skips_deploy_check(self, deps: MockDeps, tmp_path: Path):
        """PR policy doesn't trigger deploy (main didn't change)."""
        merge_results_dir = tmp_path / "data" / "ipc" / "agent-1" / "merge_results"
        merge_results_dir.mkdir(parents=True)
        fake_repo_ctx = RepoContext(slug="owner/repo", root=tmp_path, worktrees_dir=tmp_path / "wt")

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.resolve_git_policy",
                return_value=GIT_POLICY_PR,
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repo_for_group",
                return_value=fake_repo_ctx,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
                return_value={"success": True, "message": "Pushed"},
            ),
        ):
            await dispatch(
                {"type": "sync_worktree_to_main", "requestId": "req-4"},
                "agent-1",
                False,
                deps,
            )

        assert len(deps.deploy_calls) == 0


# ---------------------------------------------------------------------------
# background_merge_worktree policy dispatch tests
# ---------------------------------------------------------------------------


class TestMergeWorktreeWithPolicy:
    """Tests for the awaitable merge_worktree_with_policy()."""

    async def test_merge_policy_calls_merge_and_push(self):
        """merge-to-main policy dispatches to merge_and_push_worktree."""
        mock_repo = MagicMock()
        with (
            patch(
                "pynchy.host.git_ops.repo.resolve_repo_for_group",
                return_value=mock_repo,
            ),
            patch(
                "pynchy.host.git_ops.sync.resolve_git_policy",
                return_value=GIT_POLICY_MERGE,
            ),
            patch("pynchy.host.git_ops._worktree_merge.merge_and_push_worktree") as mock_merge,
        ):
            from pynchy.host.git_ops._worktree_merge import merge_worktree_with_policy

            await merge_worktree_with_policy("agent-1")

        mock_merge.assert_called_once_with("agent-1", mock_repo)

    async def test_pr_policy_calls_pr_workflow(self):
        """pull-request policy dispatches to host_create_pr_from_worktree."""
        mock_repo = MagicMock()
        with (
            patch(
                "pynchy.host.git_ops.repo.resolve_repo_for_group",
                return_value=mock_repo,
            ),
            patch(
                "pynchy.host.git_ops.sync.resolve_git_policy",
                return_value=GIT_POLICY_PR,
            ),
            patch("pynchy.host.git_ops.sync.host_create_pr_from_worktree") as mock_pr,
        ):
            from pynchy.host.git_ops._worktree_merge import merge_worktree_with_policy

            await merge_worktree_with_policy("agent-1")

        mock_pr.assert_called_once_with("agent-1", mock_repo)

    async def test_no_repo_access_does_nothing(self):
        """Groups without repo_access skip entirely."""
        with (
            patch(
                "pynchy.host.git_ops.repo.resolve_repo_for_group",
                return_value=None,
            ),
            patch("pynchy.host.git_ops.sync.resolve_git_policy") as mock_policy,
        ):
            from pynchy.host.git_ops._worktree_merge import merge_worktree_with_policy

            await merge_worktree_with_policy("no-repo")

        mock_policy.assert_not_called()


class TestBackgroundMergePolicy:
    def test_delegates_to_merge_worktree_with_policy(self):
        """background_merge_worktree wraps merge_worktree_with_policy in a background task."""
        group = MagicMock()
        group.folder = "agent-1"

        with patch("pynchy.utils.create_background_task") as mock_task:
            from pynchy.host.git_ops._worktree_merge import background_merge_worktree

            background_merge_worktree(group)

        mock_task.assert_called_once()
        assert "worktree-merge-agent-1" in str(mock_task.call_args)
        # Close the unawaited coroutine
        mock_task.call_args[0][0].close()

    def test_no_repo_folder_passes_through(self):
        """background_merge_worktree passes the group folder through to the coroutine."""
        group = MagicMock()
        group.folder = "no-repo"

        with patch("pynchy.utils.create_background_task") as mock_task:
            from pynchy.host.git_ops._worktree_merge import background_merge_worktree

            background_merge_worktree(group)

        # The coroutine is always created — repo check happens inside it
        mock_task.assert_called_once()
        mock_task.call_args[0][0].close()
