"""Tests for workspace rename operations.

Tests the multi-step rename_workspace() function which coordinates
database updates, filesystem renames, and git worktree moves.
Errors here could corrupt workspace state or strand data.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pynchy.db import (
    _init_test_database,
    create_task,
    get_all_tasks,
    get_task_by_id,
    set_registered_group,
    set_session,
)
from pynchy.types import RegisteredGroup
from pynchy.workspace_ops import RenameError, _rename_dir, rename_workspace


class TestRenameDir:
    """Test the _rename_dir helper which renames directories with safety checks."""

    def test_renames_existing_directory(self, tmp_path: Path):
        old = tmp_path / "old_dir"
        new = tmp_path / "new_dir"
        old.mkdir()
        (old / "file.txt").write_text("content")

        _rename_dir(old, new, "test")

        assert not old.exists()
        assert new.exists()
        assert (new / "file.txt").read_text() == "content"

    def test_skips_silently_when_source_does_not_exist(self, tmp_path: Path):
        old = tmp_path / "nonexistent"
        new = tmp_path / "new_dir"

        # Should not raise
        _rename_dir(old, new, "test")
        assert not new.exists()

    def test_raises_when_target_already_exists(self, tmp_path: Path):
        old = tmp_path / "old_dir"
        new = tmp_path / "new_dir"
        old.mkdir()
        new.mkdir()

        with pytest.raises(RenameError, match="already exists"):
            _rename_dir(old, new, "test")

    def test_error_message_includes_label(self, tmp_path: Path):
        old = tmp_path / "old_dir"
        new = tmp_path / "new_dir"
        old.mkdir()
        new.mkdir()

        with pytest.raises(RenameError, match="Target test directory"):
            _rename_dir(old, new, "test")

    def test_preserves_nested_directory_structure(self, tmp_path: Path):
        old = tmp_path / "old_dir"
        nested = old / "sub" / "deep"
        nested.mkdir(parents=True)
        (nested / "data.json").write_text("{}")

        new = tmp_path / "new_dir"
        _rename_dir(old, new, "test")

        assert (new / "sub" / "deep" / "data.json").read_text() == "{}"


# ---------------------------------------------------------------------------
# rename_workspace tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """Initialize an in-memory test database."""
    await _init_test_database()


class TestRenameWorkspace:
    """Test rename_workspace() which coordinates DB, filesystem, and git operations.

    The function updates three DB tables (registered_groups, scheduled_tasks,
    sessions), renames three filesystem directories, and optionally moves a
    git worktree + branch. Any step failing could leave state inconsistent.
    """

    async def test_renames_group_folder_in_database(self, db, tmp_path: Path):
        """DB registered_groups row updates from old folder to new folder."""
        await set_registered_group(
            "test@g.us",
            RegisteredGroup(
                name="Old Name",
                folder="old-group",
                trigger="@pynchy",
                added_at="2024-01-01",
            ),
        )

        with (
            patch("pynchy.workspace_ops.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.workspace_ops.DATA_DIR", tmp_path / "data"),
            patch("pynchy.workspace_ops.WORKTREES_DIR", tmp_path / "worktrees"),
        ):
            await rename_workspace("old-group", "new-group")

        # Verify DB was updated
        from pynchy.db import get_all_registered_groups

        groups = await get_all_registered_groups()
        folders = [g.folder for g in groups.values()]
        assert "new-group" in folders
        assert "old-group" not in folders

    async def test_renames_group_folder_and_name(self, db, tmp_path: Path):
        """When new_name is provided, both folder and name are updated in DB."""
        await set_registered_group(
            "test@g.us",
            RegisteredGroup(
                name="Old Name",
                folder="old-group",
                trigger="@pynchy",
                added_at="2024-01-01",
            ),
        )

        with (
            patch("pynchy.workspace_ops.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.workspace_ops.DATA_DIR", tmp_path / "data"),
            patch("pynchy.workspace_ops.WORKTREES_DIR", tmp_path / "worktrees"),
        ):
            await rename_workspace("old-group", "new-group", new_name="New Name")

        from pynchy.db import get_all_registered_groups

        groups = await get_all_registered_groups()
        group = next(g for g in groups.values() if g.folder == "new-group")
        assert group.name == "New Name"

    async def test_updates_scheduled_tasks(self, db, tmp_path: Path):
        """Scheduled tasks referencing old folder get updated to new folder."""
        await create_task(
            {
                "id": "task-1",
                "group_folder": "old-group",
                "chat_jid": "test@g.us",
                "prompt": "do something",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "context_mode": "isolated",
                "next_run": "2025-06-01T00:00:00",
                "status": "active",
                "created_at": "2024-01-01T00:00:00",
            }
        )

        with (
            patch("pynchy.workspace_ops.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.workspace_ops.DATA_DIR", tmp_path / "data"),
            patch("pynchy.workspace_ops.WORKTREES_DIR", tmp_path / "worktrees"),
        ):
            await rename_workspace("old-group", "new-group")

        task = await get_task_by_id("task-1")
        assert task is not None
        assert task.group_folder == "new-group"

    async def test_updates_sessions(self, db, tmp_path: Path):
        """Session entries get their group_folder updated."""
        await set_session("old-group", "session-abc")

        with (
            patch("pynchy.workspace_ops.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.workspace_ops.DATA_DIR", tmp_path / "data"),
            patch("pynchy.workspace_ops.WORKTREES_DIR", tmp_path / "worktrees"),
        ):
            await rename_workspace("old-group", "new-group")

        from pynchy.db import get_session

        # Old session should be gone, new one should exist
        old_session = await get_session("old-group")
        new_session = await get_session("new-group")
        assert old_session is None
        assert new_session == "session-abc"

    async def test_renames_group_directory(self, db, tmp_path: Path):
        """The groups/{folder} directory gets renamed."""
        groups_dir = tmp_path / "groups"
        (groups_dir / "old-group").mkdir(parents=True)
        (groups_dir / "old-group" / "CLAUDE.md").write_text("# Old")

        with (
            patch("pynchy.workspace_ops.GROUPS_DIR", groups_dir),
            patch("pynchy.workspace_ops.DATA_DIR", tmp_path / "data"),
            patch("pynchy.workspace_ops.WORKTREES_DIR", tmp_path / "worktrees"),
        ):
            await rename_workspace("old-group", "new-group")

        assert not (groups_dir / "old-group").exists()
        assert (groups_dir / "new-group" / "CLAUDE.md").read_text() == "# Old"

    async def test_renames_session_directory(self, db, tmp_path: Path):
        """The data/sessions/{folder} directory gets renamed."""
        sessions_dir = tmp_path / "data" / "sessions"
        (sessions_dir / "old-group").mkdir(parents=True)
        (sessions_dir / "old-group" / "session.json").write_text("{}")

        with (
            patch("pynchy.workspace_ops.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.workspace_ops.DATA_DIR", tmp_path / "data"),
            patch("pynchy.workspace_ops.WORKTREES_DIR", tmp_path / "worktrees"),
        ):
            await rename_workspace("old-group", "new-group")

        assert not (sessions_dir / "old-group").exists()
        assert (sessions_dir / "new-group" / "session.json").exists()

    async def test_renames_ipc_directory(self, db, tmp_path: Path):
        """The data/ipc/{folder} directory gets renamed."""
        ipc_dir = tmp_path / "data" / "ipc"
        (ipc_dir / "old-group").mkdir(parents=True)
        (ipc_dir / "old-group" / "messages").mkdir()

        with (
            patch("pynchy.workspace_ops.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.workspace_ops.DATA_DIR", tmp_path / "data"),
            patch("pynchy.workspace_ops.WORKTREES_DIR", tmp_path / "worktrees"),
        ):
            await rename_workspace("old-group", "new-group")

        assert not (ipc_dir / "old-group").exists()
        assert (ipc_dir / "new-group" / "messages").exists()

    async def test_skips_worktree_when_not_present(self, db, tmp_path: Path):
        """No error when the old worktree directory doesn't exist."""
        with (
            patch("pynchy.workspace_ops.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.workspace_ops.DATA_DIR", tmp_path / "data"),
            patch("pynchy.workspace_ops.WORKTREES_DIR", tmp_path / "worktrees"),
        ):
            # Should not raise even though worktrees/old-group doesn't exist
            await rename_workspace("old-group", "new-group")

    async def test_raises_on_worktree_move_failure(self, db, tmp_path: Path):
        """RenameError raised when git worktree move fails."""
        import subprocess

        worktrees_dir = tmp_path / "worktrees"
        (worktrees_dir / "old-group").mkdir(parents=True)

        # Mock run_git to fail on worktree move
        def fake_run_git(*args, cwd=None):
            if args[0] == "worktree" and args[1] == "move":
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr="worktree move failed"
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch("pynchy.workspace_ops.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.workspace_ops.DATA_DIR", tmp_path / "data"),
            patch("pynchy.workspace_ops.WORKTREES_DIR", worktrees_dir),
            patch("pynchy.workspace_ops.run_git", side_effect=fake_run_git),
            pytest.raises(RenameError, match="worktree move failed"),
        ):
            await rename_workspace("old-group", "new-group")

    async def test_updates_multiple_tasks_for_same_group(self, db, tmp_path: Path):
        """All tasks for the old group are updated, not just the first."""
        for i in range(3):
            await create_task(
                {
                    "id": f"task-{i}",
                    "group_folder": "old-group",
                    "chat_jid": "test@g.us",
                    "prompt": f"task {i}",
                    "schedule_type": "cron",
                    "schedule_value": "0 9 * * *",
                    "context_mode": "isolated",
                    "next_run": "2025-06-01T00:00:00",
                    "status": "active",
                    "created_at": "2024-01-01T00:00:00",
                }
            )

        with (
            patch("pynchy.workspace_ops.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.workspace_ops.DATA_DIR", tmp_path / "data"),
            patch("pynchy.workspace_ops.WORKTREES_DIR", tmp_path / "worktrees"),
        ):
            await rename_workspace("old-group", "new-group")

        tasks = await get_all_tasks()
        for task in tasks:
            assert task.group_folder == "new-group"
