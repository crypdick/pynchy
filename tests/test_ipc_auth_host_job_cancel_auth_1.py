"""Tests for IPC authorization and task scheduling."""

from __future__ import annotations

from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.state import (
    create_host_job,
    get_all_host_jobs,
    get_host_job_by_id,
)
from pynchy.workspace.api import WorkspaceProfile

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

THIRD_GROUP = WorkspaceProfile(
    jid="third@g.us",
    name="Third",
    folder="third-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)


class TestHostJobCancelAuth:
    """Tests for cancel_task routing host job IDs to delete_host_job."""

    async def test_admin_can_cancel_host_job(self, deps):
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
                "created_by": "admin-1",
                "enabled": True,
            }
        )

        await dispatch({"type": "cancel_task", "taskId": "host-cancel-1"}, "admin-1", True, deps)
        assert await get_host_job_by_id("host-cancel-1") is None

    async def test_non_admin_cannot_cancel_host_job(self, deps):
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
                "created_by": "admin-1",
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
            "admin-1",
            True,
            deps,
        )
        assert len(await get_all_host_jobs()) == 0

    async def test_missing_command_creates_no_job(self, deps):
        await dispatch(
            {
                "type": "schedule_host_job",
                "name": "no-cmd",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
            },
            "admin-1",
            True,
            deps,
        )
        assert len(await get_all_host_jobs()) == 0

    async def test_rejects_unknown_schedule_type(self, deps):
        await dispatch(
            {
                "type": "schedule_host_job",
                "name": "bad-type",
                "command": "echo hi",
                "schedule_type": "weekly-ish",
                "schedule_value": "every friday",
            },
            "admin-1",
            True,
            deps,
        )

        assert len(await get_all_host_jobs()) == 0
