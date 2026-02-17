"""Tests for IPC authorization and task scheduling."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.db import (
    _init_test_database,
    create_host_job,
    create_task,
    get_all_tasks,
    get_host_job_by_id,
    get_task_by_id,
    set_registered_group,
)
from pynchy.ipc import dispatch
from pynchy.types import RegisteredGroup

GOD_GROUP = RegisteredGroup(
    name="God",
    folder="god",
    trigger="always",
    added_at="2024-01-01T00:00:00.000Z",
    is_god=True,
)

OTHER_GROUP = RegisteredGroup(
    name="Other",
    folder="other-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)

THIRD_GROUP = RegisteredGroup(
    name="Third",
    folder="third-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)


def _test_settings(*, data_dir=None):
    return make_settings(**({"data_dir": data_dir} if data_dir is not None else {}))


class MockDeps:
    """Mock IPC dependencies."""

    def __init__(self, groups: dict[str, RegisteredGroup]):
        self._groups = groups
        self.broadcast_messages: list[tuple[str, str]] = []
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_chats: list[str] = []
        self.enqueued_checks: list[str] = []

    async def broadcast_to_channels(self, jid: str, text: str) -> None:
        self.broadcast_messages.append((jid, text))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.host_messages.append((jid, text))

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        self.system_notices.append((jid, text))

    async def send_message(self, jid: str, text: str) -> None:
        pass

    def registered_groups(self) -> dict[str, RegisteredGroup]:
        return self._groups

    def register_group(self, jid: str, group: RegisteredGroup) -> None:
        self._groups[jid] = group
        # Synchronous — in tests we won't await this
        import asyncio

        asyncio.ensure_future(set_registered_group(jid, group))

    async def sync_group_metadata(self, force: bool) -> None:
        pass

    async def get_available_groups(self) -> list[Any]:
        return []

    def write_groups_snapshot(
        self,
        group_folder: str,
        is_god: bool,
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
        return []

    def get_active_sessions(self) -> dict[str, str]:
        return {}


@pytest.fixture
async def deps():
    await _init_test_database()

    groups = {
        "god@g.us": GOD_GROUP,
        "other@g.us": OTHER_GROUP,
        "third@g.us": THIRD_GROUP,
    }

    await set_registered_group("god@g.us", GOD_GROUP)
    await set_registered_group("other@g.us", OTHER_GROUP)
    await set_registered_group("third@g.us", THIRD_GROUP)

    return MockDeps(groups)


# --- schedule_task authorization ---


class TestScheduleTaskAuth:
    async def test_god_group_can_schedule_for_another_group(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "do something",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].group_folder == "other-group"

    async def test_non_god_group_can_schedule_for_itself(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "self task",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "other@g.us",
            },
            "other-group",
            False,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].group_folder == "other-group"

    async def test_non_god_cannot_schedule_for_another_group(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "unauthorized",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "god@g.us",
            },
            "other-group",
            False,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 0

    async def test_rejects_unregistered_target_jid(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "no target",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "unknown@g.us",
            },
            "god",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 0


# --- pause_task authorization ---


class TestPauseTaskAuth:
    @pytest.fixture(autouse=True)
    async def _create_tasks(self, deps):
        await create_task(
            {
                "id": "task-god",
                "group_folder": "god",
                "chat_jid": "god@g.us",
                "prompt": "god task",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": "2025-06-01T00:00:00.000Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )
        await create_task(
            {
                "id": "task-other",
                "group_folder": "other-group",
                "chat_jid": "other@g.us",
                "prompt": "other task",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": "2025-06-01T00:00:00.000Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

    async def test_god_can_pause_any_task(self, deps):
        await dispatch({"type": "pause_task", "taskId": "task-other"}, "god", True, deps)
        task = await get_task_by_id("task-other")
        assert task is not None
        assert task.status == "paused"

    async def test_non_god_can_pause_own_task(self, deps):
        await dispatch(
            {"type": "pause_task", "taskId": "task-other"},
            "other-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-other")
        assert task is not None
        assert task.status == "paused"

    async def test_non_god_cannot_pause_other_groups_task(self, deps):
        await dispatch(
            {"type": "pause_task", "taskId": "task-god"},
            "other-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-god")
        assert task is not None
        assert task.status == "active"


# --- resume_task authorization ---


class TestResumeTaskAuth:
    @pytest.fixture(autouse=True)
    async def _create_tasks(self, deps):
        await create_task(
            {
                "id": "task-paused",
                "group_folder": "other-group",
                "chat_jid": "other@g.us",
                "prompt": "paused task",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": "2025-06-01T00:00:00.000Z",
                "status": "paused",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

    async def test_god_can_resume_any_task(self, deps):
        await dispatch({"type": "resume_task", "taskId": "task-paused"}, "god", True, deps)
        task = await get_task_by_id("task-paused")
        assert task is not None
        assert task.status == "active"

    async def test_non_god_can_resume_own_task(self, deps):
        await dispatch(
            {"type": "resume_task", "taskId": "task-paused"},
            "other-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-paused")
        assert task is not None
        assert task.status == "active"

    async def test_non_god_cannot_resume_other_groups_task(self, deps):
        await dispatch(
            {"type": "resume_task", "taskId": "task-paused"},
            "third-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-paused")
        assert task is not None
        assert task.status == "paused"


# --- cancel_task authorization ---


class TestCancelTaskAuth:
    async def test_god_can_cancel_any_task(self, deps):
        await create_task(
            {
                "id": "task-to-cancel",
                "group_folder": "other-group",
                "chat_jid": "other@g.us",
                "prompt": "cancel me",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": None,
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        await dispatch({"type": "cancel_task", "taskId": "task-to-cancel"}, "god", True, deps)
        assert await get_task_by_id("task-to-cancel") is None

    async def test_non_god_can_cancel_own_task(self, deps):
        await create_task(
            {
                "id": "task-own",
                "group_folder": "other-group",
                "chat_jid": "other@g.us",
                "prompt": "my task",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": None,
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        await dispatch(
            {"type": "cancel_task", "taskId": "task-own"},
            "other-group",
            False,
            deps,
        )
        assert await get_task_by_id("task-own") is None

    async def test_non_god_cannot_cancel_other_groups_task(self, deps):
        await create_task(
            {
                "id": "task-foreign",
                "group_folder": "god",
                "chat_jid": "god@g.us",
                "prompt": "not yours",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": None,
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        await dispatch(
            {"type": "cancel_task", "taskId": "task-foreign"},
            "other-group",
            False,
            deps,
        )
        assert await get_task_by_id("task-foreign") is not None


# --- register_group authorization ---


class TestRegisterGroupAuth:
    async def test_non_god_cannot_register_a_group(self, deps):
        await dispatch(
            {
                "type": "register_group",
                "jid": "new@g.us",
                "name": "New Group",
                "folder": "new-group",
                "trigger": "@pynchy",
            },
            "other-group",
            False,
            deps,
        )

        assert deps.registered_groups().get("new@g.us") is None


# --- IPC message authorization ---


class TestIpcMessageAuth:
    @staticmethod
    def is_message_authorized(
        source_group: str,
        is_god: bool,
        target_chat_jid: str,
        registered_groups: dict[str, RegisteredGroup],
    ) -> bool:
        target_group = registered_groups.get(target_chat_jid)
        return is_god or (target_group is not None and target_group.folder == source_group)

    def test_god_can_send_to_any_group(self, deps):
        groups = deps.registered_groups()
        assert self.is_message_authorized("god", True, "other@g.us", groups)
        assert self.is_message_authorized("god", True, "third@g.us", groups)

    def test_non_god_can_send_to_own_chat(self, deps):
        groups = deps.registered_groups()
        assert self.is_message_authorized("other-group", False, "other@g.us", groups)

    def test_non_god_cannot_send_to_other_chat(self, deps):
        groups = deps.registered_groups()
        assert not self.is_message_authorized("other-group", False, "god@g.us", groups)
        assert not self.is_message_authorized("other-group", False, "third@g.us", groups)

    def test_non_god_cannot_send_to_unregistered(self, deps):
        groups = deps.registered_groups()
        assert not self.is_message_authorized("other-group", False, "unknown@g.us", groups)

    def test_god_can_send_to_unregistered(self, deps):
        groups = deps.registered_groups()
        assert self.is_message_authorized("god", True, "unknown@g.us", groups)


# --- schedule_task schedule types ---


class TestScheduleTaskTypes:
    async def test_creates_cron_task_with_next_run(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "cron task",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].schedule_type == "cron"
        assert tasks[0].next_run is not None

    async def test_rejects_invalid_cron(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "bad cron",
                "schedule_type": "cron",
                "schedule_value": "not a cron",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        assert len(await get_all_tasks()) == 0

    async def test_creates_interval_task(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "interval task",
                "schedule_type": "interval",
                "schedule_value": "3600000",  # 1 hour in ms
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].schedule_type == "interval"
        assert tasks[0].next_run is not None

    async def test_rejects_invalid_interval_non_numeric(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "bad interval",
                "schedule_type": "interval",
                "schedule_value": "abc",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        assert len(await get_all_tasks()) == 0

    async def test_rejects_invalid_interval_zero(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "zero interval",
                "schedule_type": "interval",
                "schedule_value": "0",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        assert len(await get_all_tasks()) == 0

    async def test_rejects_invalid_once_timestamp(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "bad once",
                "schedule_type": "once",
                "schedule_value": "not-a-date",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        assert len(await get_all_tasks()) == 0


# --- context_mode ---


class TestContextMode:
    async def test_accepts_group_context(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "group context",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "group",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert tasks[0].context_mode == "group"

    async def test_accepts_isolated_context(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "isolated context",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert tasks[0].context_mode == "isolated"

    async def test_defaults_invalid_context_mode_to_isolated(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "bad context",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "bogus",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert tasks[0].context_mode == "isolated"

    async def test_defaults_missing_context_mode_to_isolated(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "no context mode",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert tasks[0].context_mode == "isolated"


# --- register_group success ---


class TestRegisterGroupSuccess:
    async def test_god_can_register_new_group(self, deps):
        await dispatch(
            {
                "type": "register_group",
                "jid": "new@g.us",
                "name": "New Group",
                "folder": "new-group",
                "trigger": "@pynchy",
            },
            "god",
            True,
            deps,
        )

        group = deps.registered_groups().get("new@g.us")
        assert group is not None
        assert group.name == "New Group"
        assert group.folder == "new-group"
        assert group.trigger == "@pynchy"

    async def test_rejects_missing_fields(self, deps):
        await dispatch(
            {
                "type": "register_group",
                "jid": "partial@g.us",
                "name": "Partial",
                # missing folder and trigger
            },
            "god",
            True,
            deps,
        )

        assert deps.registered_groups().get("partial@g.us") is None


# --- schedule_task missing fields ---


class TestScheduleTaskMissingFields:
    """schedule_task requires prompt, schedule_type, schedule_value, and targetJid.
    Missing any one should silently bail without creating a task."""

    async def test_missing_prompt_creates_no_task(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )
        assert len(await get_all_tasks()) == 0

    async def test_missing_schedule_type_creates_no_task(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "do something",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )
        assert len(await get_all_tasks()) == 0

    async def test_missing_schedule_value_creates_no_task(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "do something",
                "schedule_type": "once",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )
        assert len(await get_all_tasks()) == 0

    async def test_missing_target_jid_creates_no_task(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "do something",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
            },
            "god",
            True,
            deps,
        )
        assert len(await get_all_tasks()) == 0

    async def test_rejects_negative_interval(self, deps):
        await dispatch(
            {
                "type": "schedule_task",
                "prompt": "negative interval",
                "schedule_type": "interval",
                "schedule_value": "-1000",
                "targetJid": "other@g.us",
            },
            "god",
            True,
            deps,
        )
        assert len(await get_all_tasks()) == 0


# --- authorized_task_action edge cases ---


class TestAuthorizedTaskActionEdges:
    """Edge cases for _authorized_task_action used by pause/resume/cancel."""

    async def test_missing_task_id_is_noop(self, deps):
        """When taskId is missing from the data, nothing happens."""
        # Create a task to verify it stays untouched
        await create_task(
            {
                "id": "untouched",
                "group_folder": "other-group",
                "chat_jid": "other@g.us",
                "prompt": "should not change",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": "2025-06-01T00:00:00.000Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        # No taskId in data — should silently return
        await dispatch({"type": "pause_task"}, "god", True, deps)

        task = await get_task_by_id("untouched")
        assert task is not None
        assert task.status == "active"

    async def test_nonexistent_task_id_logs_warning(self, deps):
        """Pausing a task that doesn't exist should not crash."""
        await dispatch(
            {"type": "pause_task", "taskId": "does-not-exist"},
            "god",
            True,
            deps,
        )
        # No exception raised — the function logs a warning and returns

    async def test_cancel_nonexistent_task_is_safe(self, deps):
        """Cancelling a task that doesn't exist should not crash."""
        await dispatch(
            {"type": "cancel_task", "taskId": "ghost-task"},
            "other-group",
            False,
            deps,
        )

    async def test_unknown_ipc_type_is_ignored(self, deps):
        """Unrecognized IPC types should not crash."""
        await dispatch(
            {"type": "totally_made_up"},
            "god",
            True,
            deps,
        )


# --- deploy authorization ---


class TestDeployAuth:
    """Deploy IPC is god-only. Non-god attempts should be silently blocked."""

    async def test_non_god_cannot_deploy(self, deps):
        await dispatch(
            {
                "type": "deploy",
                "rebuildContainer": False,
                "resumePrompt": "test",
                "headSha": "abc123",
                "chatJid": "other@g.us",
            },
            "other-group",
            False,
            deps,
        )
        # No host messages sent (deploy was blocked)
        assert len(deps.host_messages) == 0

    async def test_god_deploy_invokes_finalize(self, deps):
        """God deploy with valid data calls finalize_deploy."""
        with patch(
            "pynchy.ipc._handlers_deploy.finalize_deploy", new_callable=AsyncMock
        ) as mock_finalize:
            await dispatch(
                {
                    "type": "deploy",
                    "rebuildContainer": False,
                    "resumePrompt": "Deploy complete.",
                    "headSha": "abc123",
                    "sessionId": "sess-1",
                    "chatJid": "god@g.us",
                },
                "god",
                True,
                deps,
            )
            mock_finalize.assert_called_once()
            call_kwargs = mock_finalize.call_args
            assert call_kwargs.kwargs["chat_jid"] == "god@g.us"


# --- reset_context execution ---


class TestResetContextExecution:
    """Tests for the reset_context IPC command execution paths."""

    async def test_reset_context_clears_session_and_chat(self, deps, tmp_path):
        with (
            patch(
                "pynchy.ipc._handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch("pynchy.git_ops.worktree.merge_and_push_worktree"),
        ):
            (tmp_path / "data" / "ipc" / "god").mkdir(parents=True)
            await dispatch(
                {
                    "type": "reset_context",
                    "chatJid": "god@g.us",
                    "message": "Start fresh",
                    "groupFolder": "god",
                },
                "god",
                True,
                deps,
            )

            assert "god" in deps.cleared_sessions
            assert "god@g.us" in deps.cleared_chats
            assert "god@g.us" in deps.enqueued_checks

    async def test_reset_context_writes_reset_prompt_file(self, deps, tmp_path):
        with (
            patch(
                "pynchy.ipc._handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch("pynchy.git_ops.worktree.merge_and_push_worktree"),
        ):
            (tmp_path / "data" / "ipc" / "god").mkdir(parents=True)
            await dispatch(
                {
                    "type": "reset_context",
                    "chatJid": "god@g.us",
                    "message": "Start fresh",
                    "groupFolder": "god",
                },
                "god",
                True,
                deps,
            )

            import json

            reset_file = tmp_path / "data" / "ipc" / "god" / "reset_prompt.json"
            assert reset_file.exists()
            data = json.loads(reset_file.read_text())
            assert data["message"] == "Start fresh"
            assert data["chatJid"] == "god@g.us"
            assert data["needsDirtyRepoCheck"] is True

    async def test_reset_context_rejects_missing_chat_jid(self, deps):
        """reset_context without chatJid should bail without clearing."""
        await dispatch(
            {
                "type": "reset_context",
                "message": "Start fresh",
                "groupFolder": "god",
            },
            "god",
            True,
            deps,
        )

        assert len(deps.cleared_sessions) == 0
        assert len(deps.cleared_chats) == 0

    async def test_reset_context_rejects_missing_message(self, deps):
        """reset_context without message should bail without clearing."""
        await dispatch(
            {
                "type": "reset_context",
                "chatJid": "god@g.us",
                "groupFolder": "god",
            },
            "god",
            True,
            deps,
        )

        assert len(deps.cleared_sessions) == 0

    async def test_reset_context_survives_merge_failure(self, deps, tmp_path):
        """reset_context should continue even if worktree merge fails."""
        with (
            patch(
                "pynchy.ipc._handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.git_ops.worktree.merge_and_push_worktree",
                side_effect=Exception("merge failed"),
            ),
        ):
            (tmp_path / "data" / "ipc" / "god").mkdir(parents=True)
            await dispatch(
                {
                    "type": "reset_context",
                    "chatJid": "god@g.us",
                    "message": "Start fresh",
                    "groupFolder": "god",
                },
                "god",
                True,
                deps,
            )

            # Session should still be cleared despite merge failure
            assert "god" in deps.cleared_sessions


# --- finished_work execution ---


class TestFinishedWorkExecution:
    """Tests for the finished_work IPC command."""

    async def test_finished_work_sends_host_message(self, deps):
        with patch("pynchy.workspace_config.has_project_access", return_value=False):
            await dispatch(
                {
                    "type": "finished_work",
                    "chatJid": "other@g.us",
                },
                "other-group",
                False,
                deps,
            )

            assert len(deps.host_messages) == 1
            assert deps.host_messages[0][0] == "other@g.us"
            assert "finished" in deps.host_messages[0][1].lower()

    async def test_finished_work_merges_worktree_for_project_access(self, deps):
        with patch("pynchy.git_ops.worktree.background_merge_worktree") as mock_merge:
            await dispatch(
                {
                    "type": "finished_work",
                    "chatJid": "other@g.us",
                },
                "other-group",
                False,
                deps,
            )

            mock_merge.assert_called_once()
            group_arg = mock_merge.call_args[0][0]
            assert group_arg.folder == "other-group"

    async def test_finished_work_skips_merge_for_non_project_access(self, deps):
        """When no matching group is found, background_merge_worktree is not called."""
        with patch("pynchy.git_ops.worktree.background_merge_worktree") as mock_merge:
            await dispatch(
                {
                    "type": "finished_work",
                    "chatJid": "nonexistent@g.us",
                },
                "nonexistent-group",
                False,
                deps,
            )

            mock_merge.assert_not_called()

    async def test_finished_work_rejects_missing_chat_jid(self, deps):
        """finished_work without chatJid should bail."""
        await dispatch(
            {
                "type": "finished_work",
            },
            "other-group",
            False,
            deps,
        )

        assert len(deps.host_messages) == 0

    async def test_finished_work_survives_merge_failure(self, deps):
        """finished_work should send message even if merge fails."""
        with (
            patch("pynchy.workspace_config.has_project_access", return_value=True),
            patch(
                "pynchy.git_ops.worktree.merge_and_push_worktree",
                side_effect=Exception("merge boom"),
            ),
        ):
            await dispatch(
                {
                    "type": "finished_work",
                    "chatJid": "other@g.us",
                },
                "other-group",
                False,
                deps,
            )

            # Host message should still be sent despite merge failure
            assert len(deps.host_messages) == 1


# --- create_periodic_agent authorization ---


class TestCreatePeriodicAgentAuth:
    """Tests for create_periodic_agent authorization and validation."""

    async def test_non_god_cannot_create_periodic_agent(self, deps):
        await dispatch(
            {
                "type": "create_periodic_agent",
                "name": "my-agent",
                "schedule": "0 9 * * *",
                "prompt": "do something",
            },
            "other-group",
            False,
            deps,
        )

        # No tasks should be created
        tasks = await get_all_tasks()
        assert len(tasks) == 0

    async def test_rejects_missing_required_fields(self, deps):
        """create_periodic_agent without name/schedule/prompt should bail."""
        await dispatch(
            {
                "type": "create_periodic_agent",
                "name": "my-agent",
                # missing schedule and prompt
            },
            "god",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 0

    async def test_rejects_invalid_cron_expression(self, deps):
        """create_periodic_agent with invalid cron should bail."""
        await dispatch(
            {
                "type": "create_periodic_agent",
                "name": "bad-cron-agent",
                "schedule": "not valid cron",
                "prompt": "do something",
            },
            "god",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 0


# --- host job pause/resume/cancel authorization ---


class TestHostJobPauseAuth:
    """Tests for pause_task routing host job IDs to update_host_job."""

    @pytest.fixture(autouse=True)
    async def _create_host_job(self, deps):
        await create_host_job(
            {
                "id": "host-job-1",
                "name": "test-host-job",
                "command": "echo hi",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "next_run": "2025-06-01T09:00:00Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
                "created_by": "god",
                "enabled": True,
            }
        )

    async def test_god_can_pause_host_job(self, deps):
        await dispatch({"type": "pause_task", "taskId": "host-job-1"}, "god", True, deps)
        job = await get_host_job_by_id("host-job-1")
        assert job is not None
        assert job.status == "paused"

    async def test_non_god_cannot_pause_host_job(self, deps):
        await dispatch(
            {"type": "pause_task", "taskId": "host-job-1"},
            "other-group",
            False,
            deps,
        )
        job = await get_host_job_by_id("host-job-1")
        assert job is not None
        assert job.status == "active"


class TestHostJobResumeAuth:
    """Tests for resume_task routing host job IDs to update_host_job."""

    @pytest.fixture(autouse=True)
    async def _create_host_job(self, deps):
        await create_host_job(
            {
                "id": "host-paused-1",
                "name": "paused-host-job",
                "command": "echo hi",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "next_run": "2025-06-01T09:00:00Z",
                "status": "paused",
                "created_at": "2024-01-01T00:00:00.000Z",
                "created_by": "god",
                "enabled": True,
            }
        )

    async def test_god_can_resume_host_job(self, deps):
        await dispatch({"type": "resume_task", "taskId": "host-paused-1"}, "god", True, deps)
        job = await get_host_job_by_id("host-paused-1")
        assert job is not None
        assert job.status == "active"

    async def test_non_god_cannot_resume_host_job(self, deps):
        await dispatch(
            {"type": "resume_task", "taskId": "host-paused-1"},
            "other-group",
            False,
            deps,
        )
        job = await get_host_job_by_id("host-paused-1")
        assert job is not None
        assert job.status == "paused"


class TestHostJobCancelAuth:
    """Tests for cancel_task routing host job IDs to delete_host_job."""

    async def test_god_can_cancel_host_job(self, deps):
        await create_host_job(
            {
                "id": "host-cancel-1",
                "name": "cancel-me",
                "command": "echo bye",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "next_run": "2025-06-01T09:00:00Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
                "created_by": "god",
                "enabled": True,
            }
        )

        await dispatch({"type": "cancel_task", "taskId": "host-cancel-1"}, "god", True, deps)
        assert await get_host_job_by_id("host-cancel-1") is None

    async def test_non_god_cannot_cancel_host_job(self, deps):
        await create_host_job(
            {
                "id": "host-cancel-2",
                "name": "dont-cancel-me",
                "command": "echo stay",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "next_run": "2025-06-01T09:00:00Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
                "created_by": "god",
                "enabled": True,
            }
        )

        await dispatch(
            {"type": "cancel_task", "taskId": "host-cancel-2"},
            "other-group",
            False,
            deps,
        )
        assert await get_host_job_by_id("host-cancel-2") is not None


# --- schedule_host_job missing fields ---


class TestScheduleHostJobMissingFields:
    """schedule_host_job requires name, command, schedule_type, and schedule_value."""

    async def test_missing_name_creates_no_job(self, deps):
        await dispatch(
            {
                "type": "schedule_host_job",
                "command": "echo hi",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
            },
            "god",
            True,
            deps,
        )
        from pynchy.db import get_all_host_jobs

        assert len(await get_all_host_jobs()) == 0

    async def test_missing_command_creates_no_job(self, deps):
        await dispatch(
            {
                "type": "schedule_host_job",
                "name": "no-cmd",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
            },
            "god",
            True,
            deps,
        )
        from pynchy.db import get_all_host_jobs

        assert len(await get_all_host_jobs()) == 0
