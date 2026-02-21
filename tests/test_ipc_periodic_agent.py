"""Tests for IPC create_periodic_agent and _move_to_error_dir.

Tests the create_periodic_agent IPC command which orchestrates creating folder
structure, workspace config, CLAUDE.md, chat group, and scheduled task. This is
a complex multi-step operation where partial failures need careful handling.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.db import _init_test_database, get_all_tasks
from pynchy.ipc import dispatch
from pynchy.ipc._watcher import _move_to_error_dir
from pynchy.types import WorkspaceProfile


class MockDeps:
    """Mock IPC dependencies."""

    def __init__(self, groups: dict[str, WorkspaceProfile] | None = None):
        self._groups = groups or {}
        self.broadcast_messages: list[tuple[str, str]] = []
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_chats: list[str] = []
        self.enqueued_checks: list[str] = []
        self._channels: list[Any] = []

    async def broadcast_to_channels(self, jid: str, text: str) -> None:
        self.broadcast_messages.append((jid, text))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.host_messages.append((jid, text))

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        self.system_notices.append((jid, text))

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    def register_workspace(self, profile: WorkspaceProfile) -> None:
        self._groups[profile.jid] = profile

    async def sync_group_metadata(self, force: bool) -> None:
        pass

    async def get_available_groups(self) -> list[Any]:
        return []

    def write_groups_snapshot(
        self,
        group_folder: str,
        is_admin: bool,
        available_groups: list[Any],
        registered_jids: set[str],
    ) -> None:
        pass

    async def clear_session(self, group_folder: str) -> None:
        self.cleared_sessions.append(group_folder)

    async def clear_chat_history(self, chat_jid: str) -> None:
        self.cleared_chats.append(chat_jid)

    def enqueue_message_check(self, group_jid: str) -> None:
        self.enqueued_checks.append(group_jid)

    def channels(self) -> list:
        return self._channels


@pytest.fixture
async def deps():
    await _init_test_database()
    return MockDeps(
        {
            "admin-1@g.us": WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01",
                is_admin=True,
            ),
        }
    )


class TestCreatePeriodicAgent:
    """Tests for the create_periodic_agent IPC command."""

    @staticmethod
    def _settings(tmp_path):
        return make_settings(groups_dir=tmp_path, project_root=tmp_path)

    async def test_creates_full_periodic_agent(self, deps, tmp_path, monkeypatch):
        """Should create folder, config, CLAUDE.md, chat group, and task."""
        mock_channel = AsyncMock()
        mock_channel.create_group = AsyncMock(return_value="agent@g.us")
        deps._channels = [mock_channel]

        with (
            pytest.MonkeyPatch.context() as mp,
            patch(
                "pynchy.ipc._handlers_groups.get_settings", return_value=self._settings(tmp_path)
            ),
            patch("pynchy.config.add_workspace_to_toml") as add_ws,
        ):
            mp.setenv("TZ", "UTC")
            await dispatch(
                {
                    "type": "create_periodic_agent",
                    "name": "daily-briefing",
                    "schedule": "0 9 * * *",
                    "prompt": "Compile a daily briefing",
                },
                "admin-1",
                True,
                deps,
            )
            add_ws.assert_called_once()

        # 1. Folder created
        agent_dir = tmp_path / "daily-briefing"
        assert agent_dir.exists()

        # 2. CLAUDE.md created
        claude_md = agent_dir / "CLAUDE.md"
        assert claude_md.exists()
        assert "daily-briefing" in claude_md.read_text()

        # 4. Chat group created via channel
        mock_channel.create_group.assert_called_once()

        # 5. Group registered
        assert "agent@g.us" in deps.workspaces()
        group = deps.workspaces()["agent@g.us"]
        assert group.folder == "daily-briefing"

        # 6. Scheduled task created
        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].group_folder == "daily-briefing"
        assert tasks[0].schedule_value == "0 9 * * *"
        assert tasks[0].prompt == "Compile a daily briefing"
        assert tasks[0].status == "active"

    async def test_custom_claude_md(self, deps, tmp_path, monkeypatch):
        """Custom claude_md content should be written to CLAUDE.md."""
        mock_channel = AsyncMock()
        mock_channel.create_group = AsyncMock(return_value="custom@g.us")
        deps._channels = [mock_channel]

        with (
            patch(
                "pynchy.ipc._handlers_groups.get_settings", return_value=self._settings(tmp_path)
            ),
            patch("pynchy.config.add_workspace_to_toml"),
        ):
            await dispatch(
                {
                    "type": "create_periodic_agent",
                    "name": "custom-agent",
                    "schedule": "0 8 * * 1",
                    "prompt": "Weekly report",
                    "claude_md": "# Custom Agent\nYou are a custom agent.",
                },
                "admin-1",
                True,
                deps,
            )

        claude_md = tmp_path / "custom-agent" / "CLAUDE.md"
        assert claude_md.exists()
        assert "# Custom Agent" in claude_md.read_text()

    async def test_preserves_existing_claude_md(self, deps, tmp_path, monkeypatch):
        """Should not overwrite existing CLAUDE.md."""
        # Pre-create CLAUDE.md
        agent_dir = tmp_path / "existing-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "CLAUDE.md").write_text("# Keep this content")

        mock_channel = AsyncMock()
        mock_channel.create_group = AsyncMock(return_value="existing@g.us")
        deps._channels = [mock_channel]

        with (
            patch(
                "pynchy.ipc._handlers_groups.get_settings", return_value=self._settings(tmp_path)
            ),
            patch("pynchy.config.add_workspace_to_toml"),
        ):
            await dispatch(
                {
                    "type": "create_periodic_agent",
                    "name": "existing-agent",
                    "schedule": "0 9 * * *",
                    "prompt": "Test",
                },
                "admin-1",
                True,
                deps,
            )

        # CLAUDE.md should be preserved
        assert (agent_dir / "CLAUDE.md").read_text() == "# Keep this content"

    async def test_respects_context_mode(self, deps, tmp_path, monkeypatch):
        """context_mode should be passed through to the task."""
        mock_channel = AsyncMock()
        mock_channel.create_group = AsyncMock(return_value="iso@g.us")
        deps._channels = [mock_channel]

        with (
            patch(
                "pynchy.ipc._handlers_groups.get_settings", return_value=self._settings(tmp_path)
            ),
            patch("pynchy.config.add_workspace_to_toml"),
        ):
            await dispatch(
                {
                    "type": "create_periodic_agent",
                    "name": "isolated-agent",
                    "schedule": "0 9 * * *",
                    "prompt": "Isolated task",
                    "context_mode": "isolated",
                },
                "admin-1",
                True,
                deps,
            )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].context_mode == "isolated"

    async def test_invalid_context_mode_defaults_to_group(self, deps, tmp_path, monkeypatch):
        """Invalid context_mode should default to 'group'."""
        mock_channel = AsyncMock()
        mock_channel.create_group = AsyncMock(return_value="bad@g.us")
        deps._channels = [mock_channel]

        with (
            patch(
                "pynchy.ipc._handlers_groups.get_settings", return_value=self._settings(tmp_path)
            ),
            patch("pynchy.config.add_workspace_to_toml"),
        ):
            await dispatch(
                {
                    "type": "create_periodic_agent",
                    "name": "bad-context",
                    "schedule": "0 9 * * *",
                    "prompt": "Test",
                    "context_mode": "invalid",
                },
                "admin-1",
                True,
                deps,
            )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].context_mode == "group"

    async def test_no_channel_support(self, deps, tmp_path, monkeypatch):
        """Without create_group support, should create config but no task."""
        # No channels at all
        deps._channels = []

        with (
            patch(
                "pynchy.ipc._handlers_groups.get_settings", return_value=self._settings(tmp_path)
            ),
            patch("pynchy.config.add_workspace_to_toml"),
        ):
            await dispatch(
                {
                    "type": "create_periodic_agent",
                    "name": "no-channel-agent",
                    "schedule": "0 9 * * *",
                    "prompt": "Test",
                },
                "admin-1",
                True,
                deps,
            )

        # Folder should exist even without chat group creation
        assert (tmp_path / "no-channel-agent").exists()
        # But no task (since group wasn't created)
        tasks = await get_all_tasks()
        assert len(tasks) == 0


class TestMoveToErrorDir:
    """Tests for the _move_to_error_dir helper."""

    def test_moves_file_to_error_dir(self, tmp_path):
        """Should move the file to errors/ with source_group prefix."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        source_file = ipc_dir / "test-group" / "messages" / "msg-001.json"
        source_file.parent.mkdir(parents=True)
        source_file.write_text('{"broken": true}')

        _move_to_error_dir(ipc_dir, "test-group", source_file)

        assert not source_file.exists()
        error_file = ipc_dir / "errors" / "test-group-msg-001.json"
        assert error_file.exists()
        assert error_file.read_text() == '{"broken": true}'

    def test_creates_error_dir_if_not_exists(self, tmp_path):
        """Should create the errors/ directory if it doesn't exist."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        source_file = ipc_dir / "group1" / "tasks" / "task-002.json"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("{}")

        _move_to_error_dir(ipc_dir, "group1", source_file)

        assert (ipc_dir / "errors").exists()
        assert (ipc_dir / "errors" / "group1-task-002.json").exists()

    def test_multiple_files_from_same_group(self, tmp_path):
        """Multiple error files from the same group should coexist."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()

        for i in range(3):
            f = ipc_dir / "grp" / "messages" / f"msg-{i}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f'{{"index": {i}}}')
            _move_to_error_dir(ipc_dir, "grp", f)

        error_dir = ipc_dir / "errors"
        error_files = sorted(error_dir.iterdir())
        assert len(error_files) == 3
