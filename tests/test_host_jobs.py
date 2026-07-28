"""Tests for host job scheduling via MCP tool."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import NullIpcDeps, init_test_database

from pynchy.host.container_manager.ipc import dispatch
from pynchy.host.orchestrator.temporal.host_jobs import run_database_host_job
from pynchy.host.orchestrator.temporal.runtime_state import (
    TemporalActivityInfo,
    bind_scheduler_deps,
)
from pynchy.state import (
    create_host_job,
    get_host_job_by_id,
    get_host_job_by_name,
)
from pynchy.utils import ShellResult

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class _SchedulerRuntime:
    project_root: Path


@dataclass(frozen=True)
class _SchedulerDeps:
    scheduler_runtime: _SchedulerRuntime

    def automation_memory_dir(self, _task_id: str):
        return nullcontext(None)

    def sync_automation_memory(self, _task_id: str) -> None:
        pass


@pytest.fixture(autouse=True)
async def _setup_db(tmp_path):
    await init_test_database()
    bind_scheduler_deps(_SchedulerDeps(scheduler_runtime=_SchedulerRuntime(project_root=tmp_path)))
    yield
    bind_scheduler_deps(None)


@pytest.fixture
def mock_ipc_deps():
    """IPC dependencies with real scheduled-work persistence."""

    class HostJobDeps(NullIpcDeps):
        pass

    deps = HostJobDeps()
    deps.workspaces = MagicMock()
    deps.workspaces.return_value = {
        "admin-jid": MagicMock(folder="admin-1", is_admin=True),
    }
    deps.broadcast_host_message = AsyncMock()
    return deps


class TestHostJobScheduling:
    """Test host job scheduling through MCP and database."""

    async def test_create_host_job_via_ipc_admin_group(self, mock_ipc_deps, tmp_path):
        """Admin group can schedule host jobs via IPC."""
        cwd = str(tmp_path / "cwd")
        data = {
            "type": "schedule_host_job",
            "name": "test-backup",
            "command": "echo 'backup complete'",
            "schedule_type": "cron",
            "schedule_value": "0 2 * * *",
            "cwd": cwd,
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
        assert job.cwd == cwd
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
        assert job.next_run is None

    @patch.object(_SchedulerDeps, "automation_memory_dir")
    @patch("pynchy.host.orchestrator.temporal.host_jobs.run_shell_command")
    async def test_temporal_database_host_job_activity_executes_command(
        self, mock_shell, memory_context, tmp_path
    ):
        """Temporal host-job activity executes due job commands."""
        mock_shell.return_value = ShellResult(returncode=0, stdout="Success", stderr="")
        memory_dir = tmp_path / "automation-memory/job-exec"
        memory_context.return_value = nullcontext(memory_dir)

        past_time = "2020-01-01T00:00:00"
        cwd = str(tmp_path / "cwd")
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
                "cwd": cwd,
                "timeout_seconds": 60,
                "enabled": True,
            }
        )

        result = await run_database_host_job("job-exec")

        assert result == "completed"
        mock_shell.assert_awaited_once()
        call_kwargs = mock_shell.await_args.kwargs
        assert call_kwargs["cwd"] == cwd
        assert call_kwargs["env"]["PYNCHY_AUTOMATION_MEMORY_DIR"] == str(memory_dir)
        completed_job = await get_host_job_by_id("job-exec")
        assert completed_job is not None
        assert completed_job.status == "completed"
        assert completed_job.last_run is not None
        assert completed_job.next_run is None

    @patch("pynchy.host.orchestrator.temporal.host_jobs.run_shell_command")
    async def test_temporal_database_host_job_failure_is_not_recorded_as_success(
        self, mock_shell, tmp_path
    ):
        """A failed command must make the Temporal activity fail visibly."""
        mock_shell.return_value = ShellResult(returncode=1, stdout="", stderr="boom")
        due_at = "2020-01-01T00:00:00+00:00"
        await create_host_job(
            {
                "id": "job-failure",
                "name": "failure-job",
                "command": "exit 1",
                "schedule_type": "once",
                "schedule_value": due_at,
                "next_run": due_at,
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
                "created_by": "admin-1",
                "cwd": str(tmp_path),
                "timeout_seconds": 60,
                "enabled": True,
            }
        )

        with pytest.raises(RuntimeError, match="Host job job-failure exited with code 1"):
            await run_database_host_job("job-failure")

        job = await get_host_job_by_id("job-failure")
        assert job is not None
        assert job.last_run is None
        assert job.next_run is None

    @patch("pynchy.host.orchestrator.temporal.host_jobs.run_shell_command")
    async def test_temporal_database_host_job_skips_a_stale_once_workflow(
        self, mock_shell, monkeypatch, tmp_path
    ):
        """A rescheduled row cannot run from its previous delayed workflow."""
        due_at = "2026-12-31T23:59:59+00:00"
        await create_host_job(
            {
                "id": "job-stale",
                "name": "stale-job",
                "command": "echo should-not-run",
                "schedule_type": "once",
                "schedule_value": due_at,
                "next_run": due_at,
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
                "created_by": "admin-1",
                "cwd": str(tmp_path),
                "timeout_seconds": 60,
                "enabled": True,
            }
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.temporal.runtime_state.activity.info",
            lambda: TemporalActivityInfo(workflow_id="pynchy-host-job-job-stale-old-time"),
        )

        result = await run_database_host_job("job-stale")

        assert result == "skipped"
        mock_shell.assert_not_awaited()
        job = await get_host_job_by_id("job-stale")
        assert job is not None
        assert job.status == "active"
        assert job.last_run is None

    @patch("pynchy.host.orchestrator.temporal.host_jobs.run_shell_command")
    async def test_temporal_database_host_job_skips_once_workflow_after_cron_conversion(
        self, mock_shell, monkeypatch, tmp_path
    ):
        """A delayed one-shot cannot execute a row converted to recurring work."""
        await create_host_job(
            {
                "id": "job-converted",
                "name": "converted-job",
                "command": "echo should-not-run",
                "schedule_type": "cron",
                "schedule_value": "0 3 * * *",
                "next_run": "2026-12-31T03:00:00+00:00",
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
                "created_by": "admin-1",
                "cwd": str(tmp_path),
                "timeout_seconds": 60,
                "enabled": True,
            }
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.temporal.runtime_state.activity.info",
            lambda: TemporalActivityInfo(workflow_id="pynchy-host-job-job-converted-old-time"),
        )

        result = await run_database_host_job("job-converted")

        assert result == "skipped"
        mock_shell.assert_not_awaited()
        job = await get_host_job_by_id("job-converted")
        assert job is not None
        assert job.status == "active"
        assert job.last_run is None

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
