"""Integration tests: cop_gate wired into host-mutating IPC handlers.

Verifies that each of the five host-mutating IPC handlers calls cop_gate()
before executing its side effects.  When cop_gate returns False (flagged),
the handler must bail without performing the mutation.

Tested handlers:
  - sync_worktree_to_main  (_handlers_lifecycle.py)
  - register_group          (_handlers_groups.py)
  - create_periodic_agent   (_handlers_groups.py)
  - schedule_task           (_handlers_tasks.py)
  - schedule_host_job       (_handlers_tasks.py)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.db import _init_test_database, get_all_host_jobs, get_all_tasks
from pynchy.ipc._handlers_groups import (
    _handle_create_periodic_agent,
    _handle_register_group,
)
from pynchy.ipc._handlers_lifecycle import _handle_sync_worktree_to_main
from pynchy.ipc._handlers_tasks import _handle_schedule_host_job, _handle_schedule_task
from pynchy.types import WorkspaceProfile

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

ADMIN_GROUP = WorkspaceProfile(
    jid="admin-1@g.us",
    name="Admin",
    folder="admin-1",
    trigger="always",
    added_at="2024-01-01T00:00:00.000Z",
    is_admin=True,
)

OTHER_GROUP = WorkspaceProfile(
    jid="other@g.us",
    name="Other",
    folder="other-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)


class MockDeps:
    """Minimal mock satisfying the IpcDeps protocol."""

    def __init__(self, groups: dict[str, WorkspaceProfile] | None = None):
        self._groups = groups or {}
        self.broadcast_messages: list[tuple[str, str]] = []
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.registered: list[WorkspaceProfile] = []
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
        self.registered.append(profile)
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

    def has_active_session(self, group_folder: str) -> bool:
        return False

    async def clear_session(self, group_folder: str) -> None:
        pass

    def get_active_sessions(self) -> dict[str, str]:
        return {}

    async def clear_chat_history(self, chat_jid: str) -> None:
        pass

    def enqueue_message_check(self, group_jid: str) -> None:
        pass

    def channels(self) -> list:
        return self._channels

    async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
        pass


@pytest.fixture
async def deps():
    await _init_test_database()
    return MockDeps(
        {
            "admin-1@g.us": ADMIN_GROUP,
            "other@g.us": OTHER_GROUP,
        }
    )


# ---------------------------------------------------------------------------
# 1. sync_worktree_to_main
# ---------------------------------------------------------------------------


class TestSyncWorktreeCopGate:
    """sync_worktree_to_main should call cop_gate and block on flag."""

    async def test_blocked_by_cop_writes_no_merge_result(self, deps, tmp_path):
        """When cop_gate returns False, no merge or response file is written."""
        with (
            patch(
                "pynchy.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_cop,
            patch(
                "pynchy.ipc._handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
        ):
            await _handle_sync_worktree_to_main(
                {"type": "sync_worktree_to_main", "requestId": "req-1"},
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_called_once()
        # Verify operation name is passed
        assert mock_cop.call_args.args[0] == "sync_worktree_to_main"
        # No merge_results file should exist
        result_dir = tmp_path / "data" / "ipc" / "admin-1" / "merge_results"
        assert not result_dir.exists() or not list(result_dir.iterdir())

    async def test_cop_receives_request_id(self, deps, tmp_path):
        """sync_worktree_to_main passes request_id to cop_gate (request-reply)."""
        with (
            patch(
                "pynchy.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_cop,
            patch(
                "pynchy.ipc._handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
        ):
            await _handle_sync_worktree_to_main(
                {"type": "sync_worktree_to_main", "requestId": "req-42"},
                "admin-1",
                True,
                deps,
            )

        assert mock_cop.call_args.kwargs.get("request_id") == "req-42"

    async def test_cop_approved_skips_gate(self, deps, tmp_path):
        """When _cop_approved is set, cop_gate should not be called at all."""
        with (
            patch(
                "pynchy.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
            ) as mock_cop,
            patch(
                "pynchy.ipc._handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.git_ops.repo.resolve_repo_for_group",
                return_value=None,
            ),
            patch("pynchy.ipc._handlers_lifecycle.write_ipc_response"),
        ):
            await _handle_sync_worktree_to_main(
                {
                    "type": "sync_worktree_to_main",
                    "requestId": "req-ok",
                    "_cop_approved": True,
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_not_called()


# ---------------------------------------------------------------------------
# 2. register_group
# ---------------------------------------------------------------------------


class TestRegisterGroupCopGate:
    """register_group should call cop_gate and block on flag."""

    async def test_blocked_by_cop_skips_registration(self, deps):
        """When cop_gate returns False, register_workspace is not called."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await _handle_register_group(
                {
                    "type": "register_group",
                    "jid": "new@g.us",
                    "name": "Evil Group",
                    "folder": "evil-group",
                    "trigger": "@evil",
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_called_once()
        assert mock_cop.call_args.args[0] == "register_group"
        # Group should NOT have been registered
        assert "new@g.us" not in deps.workspaces()
        assert len(deps.registered) == 0

    async def test_cop_approved_skips_gate(self, deps):
        """When _cop_approved is set, cop_gate is not called."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
        ) as mock_cop:
            await _handle_register_group(
                {
                    "type": "register_group",
                    "jid": "approved@g.us",
                    "name": "Approved Group",
                    "folder": "approved",
                    "trigger": "@pynchy",
                    "_cop_approved": True,
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_not_called()
        # Registration should proceed
        assert "approved@g.us" in deps.workspaces()

    async def test_summary_includes_key_fields(self, deps):
        """cop_gate summary should contain name, folder, and trigger."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await _handle_register_group(
                {
                    "type": "register_group",
                    "jid": "new@g.us",
                    "name": "Test Group",
                    "folder": "test-folder",
                    "trigger": "@bot",
                },
                "admin-1",
                True,
                deps,
            )

        summary = mock_cop.call_args.args[1]
        assert "Test Group" in summary
        assert "test-folder" in summary
        assert "@bot" in summary


# ---------------------------------------------------------------------------
# 3. create_periodic_agent
# ---------------------------------------------------------------------------


class TestCreatePeriodicAgentCopGate:
    """create_periodic_agent should call cop_gate and block on flag."""

    async def test_blocked_by_cop_creates_nothing(self, deps, tmp_path):
        """When cop_gate returns False, no folder, config, or task is created."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await _handle_create_periodic_agent(
                {
                    "type": "create_periodic_agent",
                    "name": "evil-agent",
                    "schedule": "0 9 * * *",
                    "prompt": "Do evil things",
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_called_once()
        assert mock_cop.call_args.args[0] == "create_periodic_agent"
        # No tasks should exist
        assert len(await get_all_tasks()) == 0
        # No new workspaces registered
        assert len(deps.registered) == 0

    async def test_summary_includes_agent_identity(self, deps):
        """cop_gate summary should include name, schedule, and prompt preview."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await _handle_create_periodic_agent(
                {
                    "type": "create_periodic_agent",
                    "name": "briefing-bot",
                    "schedule": "0 8 * * 1",
                    "prompt": "Compile a weekly briefing of all recent changes",
                },
                "admin-1",
                True,
                deps,
            )

        summary = mock_cop.call_args.args[1]
        assert "briefing-bot" in summary
        assert "0 8 * * 1" in summary
        assert "weekly briefing" in summary

    async def test_cop_approved_skips_gate(self, deps, tmp_path):
        """When _cop_approved is set, cop_gate is not called."""
        from pynchy.config_models import CommandCenterConfig

        mock_channel = AsyncMock()
        mock_channel.create_group = AsyncMock(return_value="agent@g.us")
        mock_channel.name = "connection.slack.main"
        deps._channels = [mock_channel]

        with (
            patch(
                "pynchy.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
            ) as mock_cop,
            patch(
                "pynchy.ipc._handlers_groups.get_settings",
                return_value=make_settings(
                    groups_dir=tmp_path,
                    project_root=tmp_path,
                    command_center=CommandCenterConfig(connection="connection.slack.main"),
                ),
            ),
            patch("pynchy.config.add_workspace_to_toml"),
        ):
            await _handle_create_periodic_agent(
                {
                    "type": "create_periodic_agent",
                    "name": "approved-agent",
                    "schedule": "0 9 * * *",
                    "prompt": "Do good things",
                    "_cop_approved": True,
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_not_called()
        # Agent should have been created (task exists)
        tasks = await get_all_tasks()
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# 4. schedule_task
# ---------------------------------------------------------------------------


class TestScheduleTaskCopGate:
    """schedule_task should call cop_gate and block on flag."""

    async def test_blocked_by_cop_creates_no_task(self, deps):
        """When cop_gate returns False, no task is created in the DB."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await _handle_schedule_task(
                {
                    "type": "schedule_task",
                    "prompt": "run evil command",
                    "schedule_type": "once",
                    "schedule_value": "2025-06-01T00:00:00.000Z",
                    "targetGroup": "other-group",
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_called_once()
        assert mock_cop.call_args.args[0] == "schedule_task"
        assert len(await get_all_tasks()) == 0

    async def test_summary_includes_target_and_prompt(self, deps):
        """cop_gate summary should include target group, schedule, and prompt."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await _handle_schedule_task(
                {
                    "type": "schedule_task",
                    "prompt": "delete all files",
                    "schedule_type": "cron",
                    "schedule_value": "0 3 * * *",
                    "targetGroup": "other-group",
                },
                "admin-1",
                True,
                deps,
            )

        summary = mock_cop.call_args.args[1]
        assert "other-group" in summary
        assert "cron" in summary
        assert "delete all files" in summary

    async def test_cop_approved_skips_gate(self, deps):
        """When _cop_approved is set, cop_gate is not called."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
        ) as mock_cop:
            await _handle_schedule_task(
                {
                    "type": "schedule_task",
                    "prompt": "approved task",
                    "schedule_type": "once",
                    "schedule_value": "2025-06-01T00:00:00.000Z",
                    "targetGroup": "other-group",
                    "_cop_approved": True,
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_not_called()
        tasks = await get_all_tasks()
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# 5. schedule_host_job
# ---------------------------------------------------------------------------


class TestScheduleHostJobCopGate:
    """schedule_host_job should call cop_gate and block on flag."""

    async def test_blocked_by_cop_creates_no_job(self, deps):
        """When cop_gate returns False, no host job is created."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await _handle_schedule_host_job(
                {
                    "type": "schedule_host_job",
                    "name": "evil-job",
                    "command": "rm -rf /",
                    "schedule_type": "cron",
                    "schedule_value": "0 3 * * *",
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_called_once()
        assert mock_cop.call_args.args[0] == "schedule_host_job"
        assert len(await get_all_host_jobs()) == 0

    async def test_summary_includes_command_and_schedule(self, deps):
        """cop_gate summary should include job name, command, and schedule."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await _handle_schedule_host_job(
                {
                    "type": "schedule_host_job",
                    "name": "backup-job",
                    "command": "pg_dump mydb > backup.sql",
                    "schedule_type": "interval",
                    "schedule_value": "3600000",
                },
                "admin-1",
                True,
                deps,
            )

        summary = mock_cop.call_args.args[1]
        assert "backup-job" in summary
        assert "pg_dump" in summary
        assert "interval" in summary

    async def test_cop_approved_skips_gate(self, deps):
        """When _cop_approved is set, cop_gate is not called."""
        with patch(
            "pynchy.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
        ) as mock_cop:
            await _handle_schedule_host_job(
                {
                    "type": "schedule_host_job",
                    "name": "approved-job",
                    "command": "echo hello",
                    "schedule_type": "cron",
                    "schedule_value": "0 9 * * *",
                    "_cop_approved": True,
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_not_called()
        jobs = await get_all_host_jobs()
        assert len(jobs) == 1
