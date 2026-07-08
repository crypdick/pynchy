"""Tests for host job scheduling via MCP tool."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import init_test_database

from pynchy.host.container_manager.ipc import dispatch
from pynchy.host.container_manager.ipc.deps import IpcDeps
from pynchy.state import (
    create_host_job,
    get_host_job_by_name,
)
from pynchy.utils import ShellResult


@pytest.fixture(autouse=True)
async def _setup_db():
    await init_test_database()


@pytest.fixture
def mock_ipc_deps():
    """Mock IPC dependencies."""
    deps = MagicMock(spec=IpcDeps)
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

    @patch("pynchy.host.orchestrator.temporal.host_jobs.run_shell_command")
    async def test_temporal_database_host_job_activity_executes_command(self, mock_shell):
        """Temporal host-job activity executes due job commands."""
        from pynchy.host.orchestrator.temporal.host_jobs import run_database_host_job

        mock_shell.return_value = ShellResult(returncode=0, stdout="Success", stderr="")

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

        result = await run_database_host_job("job-exec")

        assert result == "completed"
        mock_shell.assert_awaited_once()
        call_kwargs = mock_shell.await_args.kwargs
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
