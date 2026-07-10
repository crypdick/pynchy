"""Tests for IPC create_periodic_agent.

Tests the create_periodic_agent IPC command which orchestrates creating folder
structure, workspace config, CLAUDE.md, chat group, and scheduled task. This is
a complex multi-step operation where partial failures need careful handling.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, init_test_database, make_settings

from pynchy.config.models import CommandCenterConfig
from pynchy.host.container_manager.ipc import dispatch
from pynchy.host.container_manager.ipc.protocol import CreatePeriodicAgentRequest
from pynchy.state import get_all_tasks
from pynchy.types import WorkspaceProfile


class MockDeps(NullIpcDeps):
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

    async def broadcast_to_channels(self, jid: str, event: object) -> None:
        content = event.content if hasattr(event, "content") else str(event)
        self.broadcast_messages.append((jid, content))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.host_messages.append((jid, text))

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        self.system_notices.append((jid, text))

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    def register_workspace(self, profile: WorkspaceProfile) -> None:
        self._groups[profile.jid] = profile

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
    await init_test_database()
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

    def test_request_requires_explicit_profile(self):
        request = CreatePeriodicAgentRequest.from_dict(
            {
                "type": "create_periodic_agent",
                "name": "daily-briefing",
                "schedule": "0 9 * * *",
                "prompt": "Compile a daily briefing",
            }
        )

        assert request is None

    def test_request_parses_explicit_profile(self):
        request = CreatePeriodicAgentRequest.from_dict(
            {
                "type": "create_periodic_agent",
                "name": "daily-briefing",
                "profile": "pynchy-worker",
                "schedule": "0 9 * * *",
                "prompt": "Compile a daily briefing",
            }
        )

        assert request is not None
        assert request.profile == "pynchy-worker"

    @staticmethod
    def _settings(tmp_path):
        return make_settings(
            groups_dir=tmp_path,
            project_root=tmp_path,
            command_center=CommandCenterConfig(connection="main"),
        )

    @staticmethod
    def _channel(created_jid: str) -> AsyncMock:
        mock_channel = AsyncMock()
        mock_channel.create_group = AsyncMock(return_value=created_jid)
        mock_channel.name = "main"
        return mock_channel

    async def _dispatch_create_periodic_agent(
        self,
        deps: MockDeps,
        tmp_path,
        payload: dict[str, str],
    ):
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_groups.get_settings",
                return_value=self._settings(tmp_path),
            ),
            patch("pynchy.host.orchestrator.workspace_config.add_workspace_to_toml") as add_ws,
            patch("pynchy.host.orchestrator.workspace_config.add_job_to_toml"),
        ):
            await dispatch(payload, "admin-1", True, deps)
        return add_ws

    @staticmethod
    async def _single_task():
        tasks = await get_all_tasks()
        assert len(tasks) == 1
        return tasks[0]

    async def test_creates_full_periodic_agent(self, deps, tmp_path, monkeypatch):
        """Should create folder, config, CLAUDE.md, chat group, and task."""
        mock_channel = self._channel("agent@g.us")
        deps._channels = [mock_channel]

        with (
            pytest.MonkeyPatch.context() as mp,
            patch(
                "pynchy.host.container_manager.ipc.handlers_groups.get_settings",
                return_value=self._settings(tmp_path),
            ),
            patch("pynchy.host.orchestrator.workspace_config.add_workspace_to_toml") as add_ws,
            patch("pynchy.host.orchestrator.workspace_config.add_job_to_toml") as add_job,
        ):
            mp.setenv("TZ", "UTC")
            await dispatch(
                {
                    "type": "create_periodic_agent",
                    "name": "daily-briefing",
                    "profile": "pynchy-worker",
                    "schedule": "0 9 * * *",
                    "prompt": "Compile a daily briefing",
                },
                "admin-1",
                True,
                deps,
            )
            add_ws.assert_called_once()
            add_job.assert_called_once()

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
        task = await self._single_task()
        assert task.group_folder == "daily-briefing"
        assert task.schedule_value == "0 9 * * *"
        assert task.prompt == "Compile a daily briefing"
        assert task.status == "active"

    async def test_custom_claude_md(self, deps, tmp_path, monkeypatch):
        """Custom claude_md content should be written to CLAUDE.md."""
        mock_channel = self._channel("custom@g.us")
        deps._channels = [mock_channel]

        await self._dispatch_create_periodic_agent(
            deps,
            tmp_path,
            {
                "type": "create_periodic_agent",
                "name": "custom-agent",
                "profile": "pynchy-worker",
                "schedule": "0 8 * * 1",
                "prompt": "Weekly report",
                "claude_md": "# Custom Agent\nYou are a custom agent.",
            },
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

        mock_channel = self._channel("existing@g.us")
        deps._channels = [mock_channel]

        await self._dispatch_create_periodic_agent(
            deps,
            tmp_path,
            {
                "type": "create_periodic_agent",
                "name": "existing-agent",
                "profile": "pynchy-worker",
                "schedule": "0 9 * * *",
                "prompt": "Test",
            },
        )

        # CLAUDE.md should be preserved
        assert (agent_dir / "CLAUDE.md").read_text() == "# Keep this content"

    async def test_periodic_agent_task_is_isolated(self, deps, tmp_path, monkeypatch):
        """Periodic agent tasks are isolated regardless of request hints."""
        mock_channel = self._channel("iso@g.us")
        deps._channels = [mock_channel]

        await self._dispatch_create_periodic_agent(
            deps,
            tmp_path,
            {
                "type": "create_periodic_agent",
                "name": "isolated-agent",
                "profile": "pynchy-worker",
                "schedule": "0 9 * * *",
                "prompt": "Isolated task",
                "context_mode": "group",
            },
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].context_mode == "isolated"

    async def test_invalid_context_mode_still_creates_isolated_task(
        self, deps, tmp_path, monkeypatch
    ):
        """Invalid context_mode is ignored by config-backed jobs."""
        mock_channel = self._channel("bad@g.us")
        deps._channels = [mock_channel]

        await self._dispatch_create_periodic_agent(
            deps,
            tmp_path,
            {
                "type": "create_periodic_agent",
                "name": "bad-context",
                "profile": "pynchy-worker",
                "schedule": "0 9 * * *",
                "prompt": "Test",
                "context_mode": "invalid",
            },
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].context_mode == "isolated"

    async def test_no_channel_support(self, deps, tmp_path, monkeypatch):
        """Without create_group support, should create config but no task."""
        # No channels at all
        deps._channels = []

        await self._dispatch_create_periodic_agent(
            deps,
            tmp_path,
            {
                "type": "create_periodic_agent",
                "name": "no-channel-agent",
                "profile": "pynchy-worker",
                "schedule": "0 9 * * *",
                "prompt": "Test",
            },
        )

        # Folder should exist even without chat group creation
        assert (tmp_path / "no-channel-agent").exists()
        # But no task (since group wasn't created)
        tasks = await get_all_tasks()
        assert len(tasks) == 0

    async def test_empty_created_jid_does_not_register_or_schedule(self, deps, tmp_path):
        """An empty JID from create_group is invalid and must not create state."""
        mock_channel = self._channel("")
        deps._channels = [mock_channel]

        await self._dispatch_create_periodic_agent(
            deps,
            tmp_path,
            {
                "type": "create_periodic_agent",
                "name": "blank-jid-agent",
                "profile": "pynchy-worker",
                "schedule": "0 9 * * *",
                "prompt": "Test",
            },
        )

        assert "" not in deps.workspaces()
        tasks = await get_all_tasks()
        assert len(tasks) == 0
