"""Tests for IPC authorization and task scheduling.

Port of src/ipc-auth.test.ts.
"""

from __future__ import annotations

from typing import Any

import pytest

from pynchy.db import (
    _init_test_database,
    create_task,
    get_all_tasks,
    get_task_by_id,
    set_registered_group,
)
from pynchy.ipc import process_task_ipc
from pynchy.types import RegisteredGroup

MAIN_GROUP = RegisteredGroup(
    name="Main",
    folder="main",
    trigger="always",
    added_at="2024-01-01T00:00:00.000Z",
)

OTHER_GROUP = RegisteredGroup(
    name="Other",
    folder="other-group",
    trigger="@Andy",
    added_at="2024-01-01T00:00:00.000Z",
)

THIRD_GROUP = RegisteredGroup(
    name="Third",
    folder="third-group",
    trigger="@Andy",
    added_at="2024-01-01T00:00:00.000Z",
)


class MockDeps:
    """Mock IPC dependencies."""

    def __init__(self, groups: dict[str, RegisteredGroup]):
        self._groups = groups

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

    def get_available_groups(self) -> list[Any]:
        return []

    def write_groups_snapshot(
        self,
        group_folder: str,
        is_main: bool,
        available_groups: list[Any],
        registered_jids: set[str],
    ) -> None:
        pass


@pytest.fixture
async def deps():
    await _init_test_database()

    groups = {
        "main@g.us": MAIN_GROUP,
        "other@g.us": OTHER_GROUP,
        "third@g.us": THIRD_GROUP,
    }

    await set_registered_group("main@g.us", MAIN_GROUP)
    await set_registered_group("other@g.us", OTHER_GROUP)
    await set_registered_group("third@g.us", THIRD_GROUP)

    return MockDeps(groups)


# --- schedule_task authorization ---


class TestScheduleTaskAuth:
    async def test_main_group_can_schedule_for_another_group(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "do something",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].group_folder == "other-group"

    async def test_non_main_group_can_schedule_for_itself(self, deps):
        await process_task_ipc(
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

    async def test_non_main_cannot_schedule_for_another_group(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "unauthorized",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "main@g.us",
            },
            "other-group",
            False,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 0

    async def test_rejects_unregistered_target_jid(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "no target",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "unknown@g.us",
            },
            "main",
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
                "id": "task-main",
                "group_folder": "main",
                "chat_jid": "main@g.us",
                "prompt": "main task",
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

    async def test_main_can_pause_any_task(self, deps):
        await process_task_ipc({"type": "pause_task", "taskId": "task-other"}, "main", True, deps)
        task = await get_task_by_id("task-other")
        assert task is not None
        assert task.status == "paused"

    async def test_non_main_can_pause_own_task(self, deps):
        await process_task_ipc(
            {"type": "pause_task", "taskId": "task-other"},
            "other-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-other")
        assert task is not None
        assert task.status == "paused"

    async def test_non_main_cannot_pause_other_groups_task(self, deps):
        await process_task_ipc(
            {"type": "pause_task", "taskId": "task-main"},
            "other-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-main")
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

    async def test_main_can_resume_any_task(self, deps):
        await process_task_ipc({"type": "resume_task", "taskId": "task-paused"}, "main", True, deps)
        task = await get_task_by_id("task-paused")
        assert task is not None
        assert task.status == "active"

    async def test_non_main_can_resume_own_task(self, deps):
        await process_task_ipc(
            {"type": "resume_task", "taskId": "task-paused"},
            "other-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-paused")
        assert task is not None
        assert task.status == "active"

    async def test_non_main_cannot_resume_other_groups_task(self, deps):
        await process_task_ipc(
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
    async def test_main_can_cancel_any_task(self, deps):
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

        await process_task_ipc(
            {"type": "cancel_task", "taskId": "task-to-cancel"}, "main", True, deps
        )
        assert await get_task_by_id("task-to-cancel") is None

    async def test_non_main_can_cancel_own_task(self, deps):
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

        await process_task_ipc(
            {"type": "cancel_task", "taskId": "task-own"},
            "other-group",
            False,
            deps,
        )
        assert await get_task_by_id("task-own") is None

    async def test_non_main_cannot_cancel_other_groups_task(self, deps):
        await create_task(
            {
                "id": "task-foreign",
                "group_folder": "main",
                "chat_jid": "main@g.us",
                "prompt": "not yours",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": None,
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        await process_task_ipc(
            {"type": "cancel_task", "taskId": "task-foreign"},
            "other-group",
            False,
            deps,
        )
        assert await get_task_by_id("task-foreign") is not None


# --- register_group authorization ---


class TestRegisterGroupAuth:
    async def test_non_main_cannot_register_a_group(self, deps):
        await process_task_ipc(
            {
                "type": "register_group",
                "jid": "new@g.us",
                "name": "New Group",
                "folder": "new-group",
                "trigger": "@Andy",
            },
            "other-group",
            False,
            deps,
        )

        assert deps.registered_groups().get("new@g.us") is None


# --- refresh_groups authorization ---


class TestRefreshGroupsAuth:
    async def test_non_main_cannot_trigger_refresh(self, deps):
        # Should be silently blocked
        await process_task_ipc({"type": "refresh_groups"}, "other-group", False, deps)


# --- IPC message authorization ---


class TestIpcMessageAuth:
    @staticmethod
    def is_message_authorized(
        source_group: str,
        is_main: bool,
        target_chat_jid: str,
        registered_groups: dict[str, RegisteredGroup],
    ) -> bool:
        target_group = registered_groups.get(target_chat_jid)
        return is_main or (target_group is not None and target_group.folder == source_group)

    def test_main_can_send_to_any_group(self, deps):
        groups = deps.registered_groups()
        assert self.is_message_authorized("main", True, "other@g.us", groups)
        assert self.is_message_authorized("main", True, "third@g.us", groups)

    def test_non_main_can_send_to_own_chat(self, deps):
        groups = deps.registered_groups()
        assert self.is_message_authorized("other-group", False, "other@g.us", groups)

    def test_non_main_cannot_send_to_other_chat(self, deps):
        groups = deps.registered_groups()
        assert not self.is_message_authorized("other-group", False, "main@g.us", groups)
        assert not self.is_message_authorized("other-group", False, "third@g.us", groups)

    def test_non_main_cannot_send_to_unregistered(self, deps):
        groups = deps.registered_groups()
        assert not self.is_message_authorized("other-group", False, "unknown@g.us", groups)

    def test_main_can_send_to_unregistered(self, deps):
        groups = deps.registered_groups()
        assert self.is_message_authorized("main", True, "unknown@g.us", groups)


# --- schedule_task schedule types ---


class TestScheduleTaskTypes:
    async def test_creates_cron_task_with_next_run(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "cron task",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].schedule_type == "cron"
        assert tasks[0].next_run is not None

    async def test_rejects_invalid_cron(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "bad cron",
                "schedule_type": "cron",
                "schedule_value": "not a cron",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        assert len(await get_all_tasks()) == 0

    async def test_creates_interval_task(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "interval task",
                "schedule_type": "interval",
                "schedule_value": "3600000",  # 1 hour in ms
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].schedule_type == "interval"
        assert tasks[0].next_run is not None

    async def test_rejects_invalid_interval_non_numeric(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "bad interval",
                "schedule_type": "interval",
                "schedule_value": "abc",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        assert len(await get_all_tasks()) == 0

    async def test_rejects_invalid_interval_zero(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "zero interval",
                "schedule_type": "interval",
                "schedule_value": "0",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        assert len(await get_all_tasks()) == 0

    async def test_rejects_invalid_once_timestamp(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "bad once",
                "schedule_type": "once",
                "schedule_value": "not-a-date",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        assert len(await get_all_tasks()) == 0


# --- context_mode ---


class TestContextMode:
    async def test_accepts_group_context(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "group context",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "group",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert tasks[0].context_mode == "group"

    async def test_accepts_isolated_context(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "isolated context",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert tasks[0].context_mode == "isolated"

    async def test_defaults_invalid_context_mode_to_isolated(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "bad context",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "context_mode": "bogus",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert tasks[0].context_mode == "isolated"

    async def test_defaults_missing_context_mode_to_isolated(self, deps):
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "no context mode",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "targetJid": "other@g.us",
            },
            "main",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert tasks[0].context_mode == "isolated"


# --- register_group success ---


class TestRegisterGroupSuccess:
    async def test_main_can_register_new_group(self, deps):
        await process_task_ipc(
            {
                "type": "register_group",
                "jid": "new@g.us",
                "name": "New Group",
                "folder": "new-group",
                "trigger": "@Andy",
            },
            "main",
            True,
            deps,
        )

        group = deps.registered_groups().get("new@g.us")
        assert group is not None
        assert group.name == "New Group"
        assert group.folder == "new-group"
        assert group.trigger == "@Andy"

    async def test_rejects_missing_fields(self, deps):
        await process_task_ipc(
            {
                "type": "register_group",
                "jid": "partial@g.us",
                "name": "Partial",
                # missing folder and trigger
            },
            "main",
            True,
            deps,
        )

        assert deps.registered_groups().get("partial@g.us") is None
