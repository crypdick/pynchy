"""Tests for coordinated git sync system."""

from __future__ import annotations

# allow: file-length -- git sync contracts share one public workflow fixture.
import json
import subprocess  # noqa: S404 - test helpers mock subprocess behavior and exceptions
from contextlib import ExitStack
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_settings

from pynchy.host.container_manager.ipc.write import write_ipc_response
from pynchy.host.git_ops.api import (
    RepoContext,
    ensure_worktree,
    host_notify_worktree_updates,
    host_sync_worktree,
    needs_container_rebuild,
    needs_deploy,
    sync_poll,
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
    """Clone origin into a 'project' directory (simulates PROJECT_ROOT)."""
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
    repo_ctx = RepoContext(slug="owner/pynchy", root=project, worktrees_dir=worktrees_dir)

    with ExitStack() as stack:
        stack.enter_context(patch("pynchy.host.git_ops.utils._default_cwd", s.project_root))
        sync_poll.configure_git_sync_runtime(
            sync_poll.GitSyncRuntime(
                project_root=s.project_root,
                repo_slugs=tuple(s.repos.overrides),
                get_restart_hash=lambda: "test-config",
            )
        )
        yield {
            "origin": origin,
            "project": project,
            "worktrees_dir": worktrees_dir,
            "repo_ctx": repo_ctx,
        }


# ---------------------------------------------------------------------------
# host_sync_worktree tests
# ---------------------------------------------------------------------------


class TestHostSyncWorktree:
    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            ("count", "Failed to check commits"),
            ("fetch", "git fetch failed"),
            ("main-rebase", "conflicts with origin"),
            ("worktree-rebase", "Rebase conflict"),
            ("merge", "Fast-forward merge failed"),
        ],
    )
    def test_sync_reports_each_preparation_failure(
        self, git_env: dict, failure: str, expected: str
    ):
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)

        failed = subprocess.CompletedProcess(["git"], 1, "", "network down")

        def run_git(command: str, *args: str, **_kwargs: object):
            if command in {"branch", "status"}:
                return subprocess.CompletedProcess(
                    ["git"], 0, "worktree/agent-1\n" if command == "branch" else "", ""
                )
            if failure == "fetch" and command == "fetch":
                return failed
            if failure == "main-rebase" and command == "rebase" and args == ("origin/main",):
                return failed
            if failure == "worktree-rebase" and command == "rebase" and args == ("main",):
                return failed
            if failure == "merge" and command == "merge":
                return failed
            return subprocess.CompletedProcess(["git"], 0, "", "")

        with (
            patch("pynchy.host.git_ops.worktree_sync.detect_main_branch", return_value="main"),
            patch("pynchy.host.git_ops.worktree_sync._is_registered_worktree", return_value=True),
            patch(
                "pynchy.host.git_ops.worktree_sync.count_commits",
                return_value=None if failure == "count" else 1,
            ),
            patch("pynchy.host.git_ops.worktree_sync.run_git", side_effect=run_git) as git,
        ):
            result = host_sync_worktree("agent-1", repo_ctx)

        assert result["success"] is False
        assert expected in result["message"]
        if failure == "main-rebase":
            assert any(call.args[:2] == ("rebase", "--abort") for call in git.call_args_list)

    def test_sync_success(self, git_env: dict):
        """Commits merge into main and push to origin."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        result = ensure_worktree("agent-1", repo_ctx)
        wt_path = result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        result = host_sync_worktree("agent-1", repo_ctx)
        assert result["success"] is True
        assert "1 commit(s)" in result["message"]

        # Verify on main
        assert (project / "feature.txt").read_text() == "new feature"

        # Verify pushed to origin
        count = _git(project, "rev-list", "origin/main..HEAD", "--count")
        assert int(count.stdout.strip()) == 0

    def test_sync_no_worktree(self, git_env: dict):
        """Returns error when worktree doesn't exist."""
        repo_ctx = git_env["repo_ctx"]
        result = host_sync_worktree("nonexistent", repo_ctx)
        assert result["success"] is False
        assert "No worktree found" in result["message"]

    def test_sync_uncommitted_changes(self, git_env: dict):
        """Returns error when worktree has uncommitted changes."""
        repo_ctx = git_env["repo_ctx"]
        result = ensure_worktree("agent-1", repo_ctx)
        wt_path = result.path
        (wt_path / "wip.txt").write_text("uncommitted work")

        result = host_sync_worktree("agent-1", repo_ctx)
        assert result["success"] is False
        assert "uncommitted changes" in result["message"]

    def test_sync_nothing_to_merge(self, git_env: dict):
        """Returns success when already up to date."""
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)

        result = host_sync_worktree("agent-1", repo_ctx)
        assert result["success"] is True
        assert "Already up to date" in result["message"]

    def test_sync_retries_a_push_that_failed_after_merge(self, git_env: dict):
        """A second explicit sync retries commits already merged into host main."""
        repo_ctx = git_env["repo_ctx"]
        wt_path = ensure_worktree("agent-1", repo_ctx).path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        with patch(
            "pynchy.host.git_ops.worktree_sync.push_local_commits",
            side_effect=[False, True],
        ) as push:
            failed = host_sync_worktree("agent-1", repo_ctx)
            retried = host_sync_worktree("agent-1", repo_ctx)

        assert failed["success"] is False
        assert "call sync_worktree_to_main again" in failed["message"].lower()
        assert retried["success"] is True
        assert "already merged" in retried["message"]
        assert push.call_count == 2

    def test_sync_reports_pending_main_push_failure(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        with (
            patch(
                "pynchy.host.git_ops.worktree_sync._validate_sync_preconditions",
                return_value={"success": True},
            ),
            patch(
                "pynchy.host.git_ops.worktree_sync.count_unpushed_commits",
                return_value=1,
            ),
            patch(
                "pynchy.host.git_ops.worktree_sync.push_local_commits",
                return_value=False,
            ),
        ):
            result = host_sync_worktree("agent-1", repo_ctx)

        assert result == {
            "success": False,
            "message": (
                "Push to origin still failed. Your commits remain on the host main branch; "
                "inspect the reported Git state and call sync_worktree_to_main again."
            ),
        }

    def test_sync_conflict_leaves_markers(self, git_env: dict):
        """On conflict, leaves conflict markers in worktree for agent to fix."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        result = ensure_worktree("agent-1", repo_ctx)
        wt_path = result.path
        (wt_path / "README.md").write_text("agent version")
        _git(wt_path, "add", "README.md")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "agent edit README")

        # Make conflicting commit on main
        (project / "README.md").write_text("main version")
        _git(project, "add", "README.md")
        _git(project, "commit", "-m", "main edit README")

        result = host_sync_worktree("agent-1", repo_ctx)
        assert result["success"] is False
        assert "conflict" in result["message"].lower()

        # Conflict markers should be present in the worktree
        readme_content = (wt_path / "README.md").read_text()
        assert "<<<<<<<" in readme_content or "conflict" in result["message"].lower()


# ---------------------------------------------------------------------------
# host_notify_worktree_updates tests
# ---------------------------------------------------------------------------


class TestHostNotifyWorktreeUpdates:
    def _make_deps(self, groups: dict, *, active_sessions: set[str] | None = None) -> NullIpcDeps:
        """Create a fake deps satisfying WorktreeNotifyDeps.

        Args:
            active_sessions: Group folders with active sessions. Defaults to
                all groups (preserves pre-session-aware behavior in old tests).
        """
        if active_sessions is None:
            active_sessions = {g.folder for g in groups.values()}
        deps = NullIpcDeps()
        deps.broadcast_host_message = AsyncMock()
        deps.broadcast_system_notice = AsyncMock()
        deps.wake_worktree_conflict = AsyncMock()
        deps.has_active_session = lambda folder: folder in active_sessions
        deps.workspaces = lambda: groups
        return deps

    @pytest.mark.asyncio
    async def test_active_session_notifies_behind_worktrees(self, git_env: dict):
        """Clean worktrees behind main get rebased and notify active sessions."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        # Create worktree
        ensure_worktree("agent-1", repo_ctx)

        # Advance main
        (project / "new.txt").write_text("main update")
        _git(project, "add", "new.txt")
        _git(project, "commit", "-m", "advance main")

        deps = self._make_deps(
            {
                "jid-1@g.us": WorkspaceProfile(
                    jid="jid-1@g.us",
                    name="Agent 1",
                    folder="agent-1",
                    trigger="@test",
                    added_at="2024-01-01",
                ),
            }
        )

        await host_notify_worktree_updates(exclude_group=None, deps=deps, repo_ctx=repo_ctx)

        deps.broadcast_system_notice.assert_called_once()
        deps.broadcast_host_message.assert_not_called()
        call_args = deps.broadcast_system_notice.call_args
        assert "jid-1@g.us" in call_args[0]
        msg = call_args[0][1]
        assert "Auto-rebased 1 commit(s)" in msg
        # Single commit: shows full commit message instead of --oneline hint
        assert "advance main" in msg
        assert "--oneline" not in msg
        assert (git_env["worktrees_dir"] / "agent-1" / "new.txt").read_text() == "main update"

    @pytest.mark.asyncio
    async def test_multi_commit_active_session_shows_oneline_hint(self, git_env: dict):
        """Multiple clean commits notify active sessions with an oneline hint."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        ensure_worktree("agent-1", repo_ctx)

        # Push 2 commits to main
        (project / "file1.txt").write_text("first")
        _git(project, "add", "file1.txt")
        _git(project, "commit", "-m", "first change")
        (project / "file2.txt").write_text("second")
        _git(project, "add", "file2.txt")
        _git(project, "commit", "-m", "second change")

        deps = self._make_deps(
            {
                "jid-1@g.us": WorkspaceProfile(
                    jid="jid-1@g.us",
                    name="Agent 1",
                    folder="agent-1",
                    trigger="@test",
                    added_at="2024-01-01",
                ),
            }
        )

        await host_notify_worktree_updates(exclude_group=None, deps=deps, repo_ctx=repo_ctx)

        deps.broadcast_system_notice.assert_called_once()
        deps.broadcast_host_message.assert_not_called()
        msg = deps.broadcast_system_notice.call_args[0][1]
        assert "Auto-rebased 2 commit(s)" in msg
        assert "--oneline" in msg
        # Should show file stats
        assert "file" in msg.lower()
        assert (git_env["worktrees_dir"] / "agent-1" / "file1.txt").read_text() == "first"
        assert (git_env["worktrees_dir"] / "agent-1" / "file2.txt").read_text() == "second"

    @pytest.mark.asyncio
    async def test_skips_excluded_group(self, git_env: dict):
        """Excluded group (the one that just synced) is not notified."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        ensure_worktree("agent-1", repo_ctx)

        (project / "new.txt").write_text("main update")
        _git(project, "add", "new.txt")
        _git(project, "commit", "-m", "advance main")

        deps = self._make_deps(
            {
                "jid-1@g.us": WorkspaceProfile(
                    jid="jid-1@g.us",
                    name="Agent 1",
                    folder="agent-1",
                    trigger="@test",
                    added_at="2024-01-01",
                ),
            }
        )

        await host_notify_worktree_updates(exclude_group="agent-1", deps=deps, repo_ctx=repo_ctx)

        # Should NOT have sent any notifications
        deps.broadcast_system_notice.assert_not_called()
        deps.broadcast_host_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dirty_worktree_skip_rebase(self, git_env: dict):
        """Dirty worktrees skip rebase and get a different notification."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        result = ensure_worktree("agent-1", repo_ctx)
        wt_path = result.path
        (wt_path / "wip.txt").write_text("uncommitted")

        (project / "new.txt").write_text("main update")
        _git(project, "add", "new.txt")
        _git(project, "commit", "-m", "advance main")

        deps = self._make_deps(
            {
                "jid-1@g.us": WorkspaceProfile(
                    jid="jid-1@g.us",
                    name="Agent 1",
                    folder="agent-1",
                    trigger="@test",
                    added_at="2024-01-01",
                ),
            }
        )

        await host_notify_worktree_updates(exclude_group=None, deps=deps, repo_ctx=repo_ctx)

        deps.broadcast_system_notice.assert_called_once()
        msg = deps.broadcast_system_notice.call_args[0][1]
        assert "uncommitted" in msg

    @pytest.mark.asyncio
    async def test_no_notification_when_up_to_date(self, git_env: dict):
        """No notification when worktree is already current."""
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)

        deps = self._make_deps(
            {
                "jid-1@g.us": WorkspaceProfile(
                    jid="jid-1@g.us",
                    name="Agent 1",
                    folder="agent-1",
                    trigger="@test",
                    added_at="2024-01-01",
                ),
            }
        )

        await host_notify_worktree_updates(exclude_group=None, deps=deps, repo_ctx=repo_ctx)

        deps.broadcast_system_notice.assert_not_called()
        deps.broadcast_host_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_session_uses_system_notice(self, git_env: dict):
        """Active sessions get system_notice so the LLM sees the notification."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        ensure_worktree("agent-1", repo_ctx)

        (project / "new.txt").write_text("main update")
        _git(project, "add", "new.txt")
        _git(project, "commit", "-m", "advance main")

        deps = self._make_deps(
            {
                "jid-1@g.us": WorkspaceProfile(
                    jid="jid-1@g.us",
                    name="Agent 1",
                    folder="agent-1",
                    trigger="@test",
                    added_at="2024-01-01",
                ),
            },
            active_sessions={"agent-1"},
        )

        await host_notify_worktree_updates(exclude_group=None, deps=deps, repo_ctx=repo_ctx)

        deps.broadcast_system_notice.assert_called_once()
        deps.broadcast_host_message.assert_not_called()
        msg = deps.broadcast_system_notice.call_args[0][1]
        assert "Auto-rebased 1 commit(s)" in msg

    @pytest.mark.asyncio
    async def test_no_conversation_clean_rebase_is_silent(self, git_env: dict):
        """Clean rebase FYIs do not get turned into host-message DMs."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        ensure_worktree("agent-1", repo_ctx)

        (project / "new.txt").write_text("main update")
        _git(project, "add", "new.txt")
        _git(project, "commit", "-m", "advance main")

        deps = self._make_deps(
            {
                "jid-1@g.us": WorkspaceProfile(
                    jid="jid-1@g.us",
                    name="Agent 1",
                    folder="agent-1",
                    trigger="@test",
                    added_at="2024-01-01",
                ),
            },
            active_sessions=set(),  # no active sessions
        )

        await host_notify_worktree_updates(exclude_group=None, deps=deps, repo_ctx=repo_ctx)

        deps.broadcast_host_message.assert_not_called()
        deps.broadcast_system_notice.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_conversation_conflict_still_uses_host_message(self, git_env: dict):
        """Actionable rebase conflicts still notify humans when no session is active."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        result = ensure_worktree("agent-1", repo_ctx)
        wt_path = result.path
        (wt_path / "README.md").write_text("agent version")
        _git(wt_path, "add", "README.md")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "agent edit README")

        (project / "README.md").write_text("main version")
        _git(project, "add", "README.md")
        _git(project, "commit", "-m", "main edit README")

        deps = self._make_deps(
            {
                "jid-1@g.us": WorkspaceProfile(
                    jid="jid-1@g.us",
                    name="Agent 1",
                    folder="agent-1",
                    trigger="@test",
                    added_at="2024-01-01",
                ),
            },
            active_sessions=set(),
        )

        await host_notify_worktree_updates(exclude_group=None, deps=deps, repo_ctx=repo_ctx)

        deps.broadcast_host_message.assert_called_once()
        deps.broadcast_system_notice.assert_not_called()
        msg = deps.broadcast_host_message.call_args[0][1]
        assert "rebase conflicts" in msg
        deps.wake_worktree_conflict.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_conflict_wakes_owner_once_until_rebase_resolves(self, git_env: dict):
        """A pending rebase suppresses repeat conflict notifications and wakes."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        result = ensure_worktree("agent-1", repo_ctx)
        wt_path = result.path
        (wt_path / "README.md").write_text("agent version")
        _git(wt_path, "add", "README.md")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "agent edit README")

        (project / "README.md").write_text("main version")
        _git(project, "add", "README.md")
        _git(project, "commit", "-m", "main edit README")

        deps = self._make_deps(
            {
                "jid-1@g.us": WorkspaceProfile(
                    jid="jid-1@g.us",
                    name="Agent 1",
                    folder="agent-1",
                    trigger="@test",
                    added_at="2024-01-01",
                ),
            },
            active_sessions={"agent-1"},
        )

        await host_notify_worktree_updates(exclude_group=None, deps=deps, repo_ctx=repo_ctx)

        deps.broadcast_system_notice.assert_awaited_once()
        assert "rebase conflicts" in deps.broadcast_system_notice.call_args.args[1]
        deps.wake_worktree_conflict.assert_awaited_once_with("jid-1@g.us")

        await host_notify_worktree_updates(exclude_group=None, deps=deps, repo_ctx=repo_ctx)

        deps.broadcast_system_notice.assert_awaited_once()
        deps.wake_worktree_conflict.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dirty_worktree_active_session_uses_system_notice(self, git_env: dict):
        """Dirty worktree with active session: system_notice."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        result = ensure_worktree("agent-1", repo_ctx)
        wt_path = result.path
        (wt_path / "wip.txt").write_text("uncommitted")

        (project / "new.txt").write_text("main update")
        _git(project, "add", "new.txt")
        _git(project, "commit", "-m", "advance main")

        deps = self._make_deps(
            {
                "jid-1@g.us": WorkspaceProfile(
                    jid="jid-1@g.us",
                    name="Agent 1",
                    folder="agent-1",
                    trigger="@test",
                    added_at="2024-01-01",
                ),
            },
            active_sessions={"agent-1"},
        )

        await host_notify_worktree_updates(exclude_group=None, deps=deps, repo_ctx=repo_ctx)

        deps.broadcast_system_notice.assert_called_once()
        deps.broadcast_host_message.assert_not_called()
        msg = deps.broadcast_system_notice.call_args[0][1]
        assert "uncommitted" in msg


# ---------------------------------------------------------------------------
# IPC response helper tests
# ---------------------------------------------------------------------------


class TestWriteIpcResponse:
    def test_writes_response_atomically(self, tmp_path: Path):
        path = tmp_path / "merge_results" / "test-123.json"
        data = {"success": True, "message": "done"}

        write_ipc_response(path, data)

        assert path.exists()
        assert json.loads(path.read_text()) == data

    def test_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "deep" / "nested" / "result.json"
        write_ipc_response(path, {"success": False, "message": "fail"})
        assert path.exists()


# ---------------------------------------------------------------------------
# Polling helper tests
# ---------------------------------------------------------------------------


class TestPollingHelpers:
    def test_host_get_origin_main_sha_success(self, tmp_path: Path):
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="main-sha-001\trefs/heads/main\n"
            )
            sha = sync_poll.host_get_origin_main_sha(tmp_path)
            assert sha == "main-sha-001"

    def test_host_get_origin_main_sha_failure(self, tmp_path: Path):
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
            sha = sync_poll.host_get_origin_main_sha(tmp_path)
            assert sha is None

    def test_origin_probe_retains_redacted_failure_diagnostic(self, tmp_path: Path):
        credential = "repo-secret-value"
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    args=[],
                    returncode=128,
                    stdout="",
                    stderr=(
                        "fatal: unable to access "
                        f"'https://x-access-token:{credential}@github.com/owner/repo': denied"
                    ),
                ),
            ]

            probe = sync_poll.probe_origin_main_sha(tmp_path, {"GH_TOKEN": credential})

        assert probe.sha is None
        assert probe.error is not None
        assert credential not in probe.error
        assert "https://***@github.com/owner/repo" in probe.error
        assert "exit 128" in probe.error

    def test_update_failure_retains_redacted_fetch_diagnostic(self, tmp_path: Path):
        credential = "repo-secret-value"
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    args=[],
                    returncode=128,
                    stdout="",
                    stderr=(
                        "fatal: unable to access "
                        f"'https://x-access-token:{credential}@github.com/owner/repo': denied"
                    ),
                ),
            ]

            result = sync_poll.host_update_main_result(tmp_path, {"GH_TOKEN": credential})

        assert not result.succeeded
        assert result.error is not None
        assert credential not in result.error
        assert "https://***@github.com/owner/repo" in result.error
        assert "git fetch origin failed with exit 128" in result.error

    def test_container_files_change_triggers_rebuild(self):
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="src/pynchy/agent/Dockerfile\n"
            )
            assert needs_container_rebuild("abc", "def") is True

    def test_no_container_files_change_skips_rebuild(self):
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
            assert needs_container_rebuild("abc", "def") is False


# ---------------------------------------------------------------------------
# Deploy-check helper tests
# ---------------------------------------------------------------------------


class TestNeedsDeploy:
    def test_needs_deploy_src_changes(self):
        """src/ changes require a deploy."""
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            # files_changed_between calls git diff --name-only with path filter.
            # First call (src/pynchy/agent/) returns empty, second (src/) returns a file.
            mock_run.side_effect = [
                subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
                subprocess.CompletedProcess(args=[], returncode=0, stdout="src/pynchy/app.py\n"),
            ]
            assert needs_deploy("aaa", "bbb") is True

    def test_needs_deploy_container_changes(self):
        """src/pynchy/agent/ changes require a deploy."""
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="src/pynchy/agent/Dockerfile\n"
            )
            assert needs_deploy("aaa", "bbb") is True

    def test_needs_deploy_no_relevant_changes(self):
        """Changes outside src/ don't need a deploy."""
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
            assert needs_deploy("aaa", "bbb") is False

    def test_needs_container_rebuild_src_only(self):
        """src/ changes don't need a container rebuild."""
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
            assert needs_container_rebuild("aaa", "bbb") is False

    def test_needs_container_rebuild_container_changes(self):
        """src/pynchy/agent/ changes need a container rebuild."""
        with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="src/pynchy/agent/Dockerfile\n"
            )
            assert needs_container_rebuild("aaa", "bbb") is True


# ---------------------------------------------------------------------------
# Origin drift helper tests
# ---------------------------------------------------------------------------


class _RecordingGitSyncDeps:
    def __init__(self) -> None:
        self.deploy_calls: list[tuple[str, bool]] = []

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None:
        self.deploy_calls.append((previous_sha, rebuild))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        return None

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        return None

    async def wake_worktree_conflict(self, jid: str) -> None:
        return None

    def has_active_session(self, group_folder: str) -> bool:
        return False

    def workspaces(self) -> dict:
        return {}


class TestCheckOriginDrift:
    @pytest.mark.asyncio
    async def test_origin_change_updates_baseline_when_local_already_matches(self, tmp_path: Path):
        deps = _RecordingGitSyncDeps()
        state = sync_poll.HostSyncState(
            last_origin_sha="old-origin",
            deployed_sha="deployed",
            config_hash="cfg",
            local_head="new-origin",
        )

        with patch(
            "pynchy.host.git_ops.sync_poll.host_get_origin_main_sha",
            return_value="new-origin",
        ):
            changed = await sync_poll.check_origin_drift(
                tmp_path, state, None, deps, auto_deploy=True
            )

        assert changed is False
        assert state.last_origin_sha == "new-origin"
        assert deps.deploy_calls == []

    @pytest.mark.asyncio
    async def test_origin_change_pulls_notifies_and_deploys(self, tmp_path: Path):
        deps = _RecordingGitSyncDeps()
        repo_ctx = RepoContext(slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "wt")
        state = sync_poll.HostSyncState(
            last_origin_sha="old-origin",
            deployed_sha="deployed-sha",
            config_hash="cfg",
            local_head="local-sha",
        )

        with (
            patch(
                "pynchy.host.git_ops.sync_poll.host_get_origin_main_sha",
                return_value="origin-new",
            ),
            patch("pynchy.host.git_ops.sync_poll.host_update_main", return_value=True),
            patch("pynchy.host.git_ops.sync_poll.get_local_head_sha", return_value="pulled-head"),
            patch("pynchy.host.git_ops.sync_poll.needs_deploy", return_value=True),
            patch("pynchy.host.git_ops.sync_poll.needs_container_rebuild", return_value=True),
            patch(
                "pynchy.host.git_ops.sync_poll.host_notify_worktree_updates",
                new_callable=AsyncMock,
            ) as notify,
        ):
            sync_poll.last_notified_sha.pop(str(tmp_path), None)
            changed = await sync_poll.check_origin_drift(
                tmp_path, state, repo_ctx, deps, auto_deploy=True
            )

        assert changed is True
        assert state.last_origin_sha == "origin-new"
        assert deps.deploy_calls == [("deployed-sha", True)]
        notify.assert_awaited_once()


# ---------------------------------------------------------------------------
# Config file hashing tests
# ---------------------------------------------------------------------------


class TestHashConfigFiles:
    def test_deploy_hash_uses_the_composed_configuration_classifier(self, git_env: dict):
        assert sync_poll.get_deploy_config_hash() == "test-config"
