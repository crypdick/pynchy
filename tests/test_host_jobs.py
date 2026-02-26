"""Tests for host job scheduling via MCP tool."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.db import (
    _init_test_database,
    create_host_job,
    get_due_host_jobs,
    get_host_job_by_name,
)
from pynchy.ipc import dispatch
from pynchy.task_scheduler import _poll_database_host_jobs


@pytest.fixture(autouse=True)
async def _setup_db():
    await _init_test_database()


@pytest.fixture
def mock_ipc_deps():
    """Mock IPC dependencies."""
    deps = MagicMock()
    deps.workspaces.return_value = {
        "admin-jid": MagicMock(folder="admin-1", is_admin=True),
    }
    deps.broadcast_host_message = AsyncMock()
    return deps


class TestHostJobScheduling:
    """Test host job scheduling through MCP and database."""

    async def test_create_host_job_via_ipc_admin_group(self, mock_ipc_deps):
        """Admin group can schedule host jobs via IPC."""
        data = {
            "type": "schedule_host_job",
            "name": "test-backup",
            "command": "echo 'backup complete'",
            "schedule_type": "cron",
            "schedule_value": "0 2 * * *",
            "cwd": "/tmp",
            "timeout_seconds": 300,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        await dispatch(data, "admin-1", True, mock_ipc_deps)

        # Verify job was created
        job = await get_host_job_by_name("test-backup")
        assert job is not None
        assert job.command == "echo 'backup complete'"
        assert job.schedule_type == "cron"
        assert job.schedule_value == "0 2 * * *"
        assert job.cwd == "/tmp"
        assert job.timeout_seconds == 300
        assert job.created_by == "admin-1"
        assert job.enabled is True

    async def test_create_host_job_rejects_non_admin(self, mock_ipc_deps):
        """Non-admin groups cannot schedule host jobs."""
        mock_ipc_deps.workspaces.return_value = {
            "user-jid": MagicMock(folder="user-group", is_admin=False),
        }

        data = {
            "type": "schedule_host_job",
            "name": "sneaky-job",
            "command": "rm -rf /",
            "schedule_type": "once",
            "schedule_value": "2026-12-31T23:59:59",
            "timestamp": datetime.now(UTC).isoformat(),
        }

        await dispatch(data, "user-group", False, mock_ipc_deps)

        # Verify job was NOT created
        job = await get_host_job_by_name("sneaky-job")
        assert job is None

    async def test_create_once_host_job(self, mock_ipc_deps):
        """Can schedule one-time host jobs."""
        future_time = "2026-12-31T23:59:59"
        data = {
            "type": "schedule_host_job",
            "name": "year-end-report",
            "command": "python generate_report.py",
            "schedule_type": "once",
            "schedule_value": future_time,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        await dispatch(data, "admin-1", True, mock_ipc_deps)

        job = await get_host_job_by_name("year-end-report")
        assert job is not None
        assert job.schedule_type == "once"
        assert job.next_run == future_time

    async def test_get_due_host_jobs(self):
        """get_due_host_jobs returns jobs that are due."""
        past_time = "2020-01-01T00:00:00"
        future_time = "2099-12-31T23:59:59"

        await create_host_job(
            {
                "id": "job-due",
                "name": "due-job",
                "command": "echo due",
                "schedule_type": "once",
                "schedule_value": past_time,
                "next_run": past_time,
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
                "created_by": "admin-1",
                "enabled": True,
            }
        )

        await create_host_job(
            {
                "id": "job-future",
                "name": "future-job",
                "command": "echo future",
                "schedule_type": "once",
                "schedule_value": future_time,
                "next_run": future_time,
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
                "created_by": "admin-1",
                "enabled": True,
            }
        )

        due_jobs = await get_due_host_jobs()
        assert len(due_jobs) == 1
        assert due_jobs[0].name == "due-job"

    async def test_disabled_host_jobs_not_returned(self):
        """Disabled host jobs are not returned by get_due_host_jobs."""
        past_time = "2020-01-01T00:00:00"

        await create_host_job(
            {
                "id": "job-disabled",
                "name": "disabled-job",
                "command": "echo disabled",
                "schedule_type": "once",
                "schedule_value": past_time,
                "next_run": past_time,
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
                "created_by": "admin-1",
                "enabled": False,
            }
        )

        due_jobs = await get_due_host_jobs()
        assert len(due_jobs) == 0

    @patch("pynchy.task_scheduler.asyncio.create_subprocess_shell")
    async def test_poll_database_host_jobs_executes_command(self, mock_subprocess):
        """_poll_database_host_jobs executes due job commands."""
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"Success", b"")
        mock_subprocess.return_value = mock_process

        past_time = "2020-01-01T00:00:00"
        await create_host_job(
            {
                "id": "job-exec",
                "name": "exec-job",
                "command": "echo 'test command'",
                "schedule_type": "once",
                "schedule_value": past_time,
                "next_run": past_time,
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
                "created_by": "admin-1",
                "cwd": "/tmp",
                "timeout_seconds": 60,
                "enabled": True,
            }
        )

        await _poll_database_host_jobs()

        mock_subprocess.assert_called_once()
        call_kwargs = mock_subprocess.call_args[1]
        assert call_kwargs["cwd"] == "/tmp"

    async def test_host_job_validates_invalid_cron(self, mock_ipc_deps):
        """Host job creation rejects invalid cron expressions."""
        data = {
            "type": "schedule_host_job",
            "name": "bad-cron",
            "command": "echo bad",
            "schedule_type": "cron",
            "schedule_value": "invalid cron",
            "timestamp": datetime.now(UTC).isoformat(),
        }

        await dispatch(data, "admin-1", True, mock_ipc_deps)

        job = await get_host_job_by_name("bad-cron")
        assert job is None

    async def test_host_job_validates_invalid_timestamp(self, mock_ipc_deps):
        """Host job creation rejects invalid timestamps."""
        data = {
            "type": "schedule_host_job",
            "name": "bad-timestamp",
            "command": "echo bad",
            "schedule_type": "once",
            "schedule_value": "not-a-timestamp",
            "timestamp": datetime.now(UTC).isoformat(),
        }

        await dispatch(data, "admin-1", True, mock_ipc_deps)

        job = await get_host_job_by_name("bad-timestamp")
        assert job is None
