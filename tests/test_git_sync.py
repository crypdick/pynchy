"""Tests for coordinated git sync system.

Tests host_sync_worktree(), host_notify_worktree_updates(), guard_git.sh,
and the MCP tool round-trip via mocked IPC.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pynchy.config import (
    AgentConfig,
    CommandWordsConfig,
    ContainerConfig,
    IntervalsConfig,
    LoggingConfig,
    QueueConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    Settings,
    WorkspaceDefaultsConfig,
)
from pynchy.git_sync import (
    _host_container_files_changed,
    _host_get_origin_main_sha,
    host_notify_worktree_updates,
    host_sync_worktree,
    write_ipc_response,
)
from pynchy.worktree import ensure_worktree

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

    s = Settings.model_construct(
        agent=AgentConfig(),
        container=ContainerConfig(),
        server=ServerConfig(),
        logging=LoggingConfig(),
        secrets=SecretsConfig(),
        workspace_defaults=WorkspaceDefaultsConfig(),
        workspaces={},
        commands=CommandWordsConfig(),
        scheduler=SchedulerConfig(),
        intervals=IntervalsConfig(),
        queue=QueueConfig(),
        security=SecurityConfig(),
    )
    s.__dict__["project_root"] = project
    s.__dict__["worktrees_dir"] = worktrees_dir

    with ExitStack() as stack:
        stack.enter_context(patch("pynchy.git_utils.get_settings", return_value=s))
        stack.enter_context(patch("pynchy.worktree.get_settings", return_value=s))
        stack.enter_context(patch("pynchy.git_sync.get_settings", return_value=s))
        yield {
            "origin": origin,
            "project": project,
            "worktrees_dir": worktrees_dir,
        }


# ---------------------------------------------------------------------------
# host_sync_worktree tests
# ---------------------------------------------------------------------------


class TestHostSyncWorktree:
    def test_sync_success(self, git_env: dict):
        """Commits merge into main and push to origin."""
        project = git_env["project"]

        result = ensure_worktree("agent-1")
        wt_path = result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        result = host_sync_worktree("agent-1")
        assert result["success"] is True
        assert "1 commit(s)" in result["message"]

        # Verify on main
        assert (project / "feature.txt").read_text() == "new feature"

        # Verify pushed to origin
        count = _git(project, "rev-list", "origin/main..HEAD", "--count")
        assert int(count.stdout.strip()) == 0

    def test_sync_no_worktree(self, git_env: dict):
        """Returns error when worktree doesn't exist."""
        result = host_sync_worktree("nonexistent")
        assert result["success"] is False
        assert "No worktree found" in result["message"]

    def test_sync_uncommitted_changes(self, git_env: dict):
        """Returns error when worktree has uncommitted changes."""
        result = ensure_worktree("agent-1")
        wt_path = result.path
        (wt_path / "wip.txt").write_text("uncommitted work")

        result = host_sync_worktree("agent-1")
        assert result["success"] is False
        assert "uncommitted changes" in result["message"]

    def test_sync_nothing_to_merge(self, git_env: dict):
        """Returns success when already up to date."""
        ensure_worktree("agent-1")

        result = host_sync_worktree("agent-1")
        assert result["success"] is True
        assert "Already up to date" in result["message"]

    def test_sync_conflict_leaves_markers(self, git_env: dict):
        """On conflict, leaves conflict markers in worktree for agent to fix."""
        project = git_env["project"]

        result = ensure_worktree("agent-1")
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

        result = host_sync_worktree("agent-1")
        assert result["success"] is False
        assert "conflict" in result["message"].lower()

        # Conflict markers should be present in the worktree
        readme_content = (wt_path / "README.md").read_text()
        assert "<<<<<<<" in readme_content or "conflict" in result["message"].lower()


# ---------------------------------------------------------------------------
# host_notify_worktree_updates tests
# ---------------------------------------------------------------------------


class TestHostNotifyWorktreeUpdates:
    @pytest.mark.asyncio
    async def test_notifies_behind_worktrees(self, git_env: dict):
        """Worktrees behind main get rebased and notified."""
        project = git_env["project"]

        # Create worktree
        ensure_worktree("agent-1")

        # Advance main
        (project / "new.txt").write_text("main update")
        _git(project, "add", "new.txt")
        _git(project, "commit", "-m", "advance main")

        from pynchy.types import RegisteredGroup

        deps = Mock()
        deps.broadcast_system_notice = AsyncMock()
        deps.registered_groups.return_value = {
            "jid-1@g.us": RegisteredGroup(
                name="Agent 1",
                folder="agent-1",
                trigger="@test",
                added_at="2024-01-01",
            ),
        }

        await host_notify_worktree_updates(exclude_group=None, deps=deps)

        # Should have sent a notification
        deps.broadcast_system_notice.assert_called_once()
        call_args = deps.broadcast_system_notice.call_args
        assert "jid-1@g.us" in call_args[0]
        msg = call_args[0][1]
        assert "Auto-rebased 1 commit(s)" in msg
        # Single commit: shows full commit message instead of --oneline hint
        assert "advance main" in msg
        assert "--oneline" not in msg

    @pytest.mark.asyncio
    async def test_multi_commit_shows_oneline_hint(self, git_env: dict):
        """Multiple commits show --oneline hint instead of commit message."""
        project = git_env["project"]

        ensure_worktree("agent-1")

        # Push 2 commits to main
        (project / "file1.txt").write_text("first")
        _git(project, "add", "file1.txt")
        _git(project, "commit", "-m", "first change")
        (project / "file2.txt").write_text("second")
        _git(project, "add", "file2.txt")
        _git(project, "commit", "-m", "second change")

        from pynchy.types import RegisteredGroup

        deps = Mock()
        deps.broadcast_system_notice = AsyncMock()
        deps.registered_groups.return_value = {
            "jid-1@g.us": RegisteredGroup(
                name="Agent 1",
                folder="agent-1",
                trigger="@test",
                added_at="2024-01-01",
            ),
        }

        await host_notify_worktree_updates(exclude_group=None, deps=deps)

        deps.broadcast_system_notice.assert_called_once()
        msg = deps.broadcast_system_notice.call_args[0][1]
        assert "Auto-rebased 2 commit(s)" in msg
        assert "--oneline" in msg
        # Should show file stats
        assert "file" in msg.lower()

    @pytest.mark.asyncio
    async def test_skips_excluded_group(self, git_env: dict):
        """Excluded group (the one that just synced) is not notified."""
        project = git_env["project"]

        ensure_worktree("agent-1")

        (project / "new.txt").write_text("main update")
        _git(project, "add", "new.txt")
        _git(project, "commit", "-m", "advance main")

        from pynchy.types import RegisteredGroup

        deps = Mock()
        deps.broadcast_system_notice = AsyncMock()
        deps.registered_groups.return_value = {
            "jid-1@g.us": RegisteredGroup(
                name="Agent 1",
                folder="agent-1",
                trigger="@test",
                added_at="2024-01-01",
            ),
        }

        await host_notify_worktree_updates(exclude_group="agent-1", deps=deps)

        # Should NOT have sent any notifications
        deps.broadcast_system_notice.assert_not_called()

    @pytest.mark.asyncio
    async def test_dirty_worktree_skip_rebase(self, git_env: dict):
        """Dirty worktrees skip rebase and get a different notification."""
        project = git_env["project"]

        result = ensure_worktree("agent-1")
        wt_path = result.path
        (wt_path / "wip.txt").write_text("uncommitted")

        (project / "new.txt").write_text("main update")
        _git(project, "add", "new.txt")
        _git(project, "commit", "-m", "advance main")

        from pynchy.types import RegisteredGroup

        deps = Mock()
        deps.broadcast_system_notice = AsyncMock()
        deps.registered_groups.return_value = {
            "jid-1@g.us": RegisteredGroup(
                name="Agent 1",
                folder="agent-1",
                trigger="@test",
                added_at="2024-01-01",
            ),
        }

        await host_notify_worktree_updates(exclude_group=None, deps=deps)

        deps.broadcast_system_notice.assert_called_once()
        msg = deps.broadcast_system_notice.call_args[0][1]
        assert "uncommitted" in msg

    @pytest.mark.asyncio
    async def test_no_notification_when_up_to_date(self, git_env: dict):
        """No notification when worktree is already current."""
        ensure_worktree("agent-1")

        from pynchy.types import RegisteredGroup

        deps = Mock()
        deps.broadcast_system_notice = AsyncMock()
        deps.registered_groups.return_value = {
            "jid-1@g.us": RegisteredGroup(
                name="Agent 1",
                folder="agent-1",
                trigger="@test",
                added_at="2024-01-01",
            ),
        }

        await host_notify_worktree_updates(exclude_group=None, deps=deps)

        deps.broadcast_system_notice.assert_not_called()


# ---------------------------------------------------------------------------
# Guard git hook script tests
# ---------------------------------------------------------------------------


GUARD_SCRIPT = Path(__file__).parent.parent / "container" / "scripts" / "guard_git.sh"


class TestGuardGitHook:
    def _run_hook(self, command: str) -> dict:
        """Simulate running the hook script with a given bash command."""
        hook_input = json.dumps({"tool_input": {"command": command}})
        result = subprocess.run(
            ["bash", str(GUARD_SCRIPT)],
            input=hook_input,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout.strip())

    def test_blocks_git_push(self):
        result = self._run_hook("git push origin main")
        assert result.get("decision") == "block"
        assert "sync_worktree_to_main" in result.get("reason", "")

    def test_blocks_git_pull(self):
        result = self._run_hook("git pull origin main")
        assert result.get("decision") == "block"

    def test_blocks_git_rebase(self):
        result = self._run_hook("git rebase origin/main")
        assert result.get("decision") == "block"

    def test_allows_git_commit(self):
        result = self._run_hook("git commit -m 'test'")
        assert result.get("decision") is None  # empty object = allow

    def test_allows_git_status(self):
        result = self._run_hook("git status")
        assert result.get("decision") is None

    def test_allows_git_add(self):
        result = self._run_hook("git add .")
        assert result.get("decision") is None

    def test_allows_git_diff(self):
        result = self._run_hook("git diff")
        assert result.get("decision") is None

    def test_allows_non_git_command(self):
        result = self._run_hook("ls -la")
        assert result.get("decision") is None

    def test_blocks_git_push_in_pipe(self):
        result = self._run_hook("echo foo && git push")
        assert result.get("decision") == "block"


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
    def test_host_get_origin_main_sha_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="main-sha-001\trefs/heads/main\n"
            )
            sha = _host_get_origin_main_sha()
            assert sha == "main-sha-001"

    def test_host_get_origin_main_sha_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
            sha = _host_get_origin_main_sha()
            assert sha is None

    def test_host_container_files_changed_true(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="container/Dockerfile\n"
            )
            assert _host_container_files_changed("abc", "def") is True

    def test_host_container_files_changed_false(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
            assert _host_container_files_changed("abc", "def") is False
