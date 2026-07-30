"""Tests for Temporal-backed scheduled-work status."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from pynchy.host.orchestrator.temporal import status as temporal_status
from pynchy.scheduling.api import (
    HostJob,
    ScheduledTask,
    SessionPolicy,
)


@dataclass(frozen=True)
class _ScheduleInfo:
    next_action_times: list[datetime]


@dataclass(frozen=True)
class _ScheduleState:
    paused: bool = False


@dataclass(frozen=True)
class _Schedule:
    state: _ScheduleState


@dataclass(frozen=True)
class _ScheduleDescription:
    info: _ScheduleInfo
    schedule: _Schedule


@dataclass(frozen=True)
class _WorkflowDescription:
    execution_time: datetime | None
    status: WorkflowExecutionStatus | None


class _ScheduleHandle:
    def __init__(self, description: _ScheduleDescription) -> None:
        self._description = description

    async def describe(self, **_kwargs: object) -> _ScheduleDescription:
        return self._description


class _WorkflowHandle:
    def __init__(self, description: _WorkflowDescription) -> None:
        self._description = description

    async def describe(self, **_kwargs: object) -> _WorkflowDescription:
        return self._description


class _TemporalClient:
    def __init__(
        self,
        schedule_description: _ScheduleDescription | None = None,
        workflow_description: _WorkflowDescription | None = None,
    ) -> None:
        self._schedule_description = schedule_description
        self._workflow_description = workflow_description

    def get_schedule_handle(self, _schedule_id: str) -> _ScheduleHandle:
        assert self._schedule_description is not None
        return _ScheduleHandle(self._schedule_description)

    def get_workflow_handle(self, _workflow_id: str) -> _WorkflowHandle:
        assert self._workflow_description is not None
        return _WorkflowHandle(self._workflow_description)


class _ErrorHandle:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def describe(self, **_kwargs: object) -> None:
        raise self._error


class _ErrorClient:
    def __init__(self, error: Exception) -> None:
        self._handle = _ErrorHandle(error)

    def get_schedule_handle(self, _schedule_id: str) -> _ErrorHandle:
        return self._handle

    def get_workflow_handle(self, _workflow_id: str) -> _ErrorHandle:
        return self._handle


def _recurring_task() -> ScheduledTask:
    return ScheduledTask(
        id="task-1",
        group_folder="admin",
        chat_jid="slack:CADMIN",
        prompt="Check scheduled work.",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        session_policy=SessionPolicy.RESET_BEFORE_RUN,
        next_run="2026-01-01T09:00:00+00:00",
        status="active",
    )


@pytest.mark.asyncio
async def test_recurring_state_uses_temporal_action_time(monkeypatch):
    temporal_next_run = datetime(2026, 1, 1, 17, 0, tzinfo=UTC)
    client = _TemporalClient(
        schedule_description=_ScheduleDescription(
            info=_ScheduleInfo(next_action_times=[temporal_next_run]),
            schedule=_Schedule(state=_ScheduleState()),
        )
    )
    monkeypatch.setattr(temporal_status.Client, "connect", AsyncMock(return_value=client))

    states = await temporal_status.get_temporal_orchestration_states(
        [_recurring_task()], [], "localhost:7233", "default"
    )

    state = states["task", "task-1"]
    assert state["source"] == "temporal"
    assert state["state"] == "scheduled"
    assert state["next_run"] == temporal_next_run.isoformat()


@pytest.mark.asyncio
async def test_unavailable_temporal_has_no_sqlite_next_run_fallback(monkeypatch):
    monkeypatch.setattr(
        temporal_status.Client,
        "connect",
        AsyncMock(side_effect=RuntimeError("connection refused")),
    )

    states = await temporal_status.get_temporal_orchestration_states(
        [_recurring_task()], [], "localhost:7233", "default"
    )

    state = states["task", "task-1"]
    assert state["state"] == "unavailable"
    assert state["next_run"] is None
    assert state["error"] == "connection refused"


@pytest.mark.asyncio
async def test_once_job_uses_delayed_temporal_workflow_execution_time(monkeypatch):
    due_at = datetime.now(UTC) + timedelta(minutes=5)
    client = _TemporalClient(
        workflow_description=_WorkflowDescription(
            execution_time=due_at,
            status=WorkflowExecutionStatus.RUNNING,
        )
    )
    job = HostJob(
        id="host-job-1",
        name="once-job",
        command="echo once",
        schedule_type="once",
        schedule_value=due_at.isoformat(),
        created_by="admin",
        status="active",
    )
    monkeypatch.setattr(temporal_status.Client, "connect", AsyncMock(return_value=client))

    states = await temporal_status.get_temporal_orchestration_states(
        [], [job], "localhost:7233", "default"
    )

    state = states["host_job", "host-job-1"]
    assert state["state"] == "delayed"
    assert state["next_run"] == due_at.isoformat()


@pytest.mark.asyncio
async def test_inactive_tasks_and_disabled_jobs_do_not_connect_to_temporal():
    paused_task = replace(_recurring_task(), status="paused")
    disabled_job = HostJob(
        id="disabled-job",
        name="disabled",
        command="echo disabled",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        created_by="admin",
        enabled=False,
    )

    states = await temporal_status.get_temporal_orchestration_states(
        [paused_task], [disabled_job], "unused", "unused"
    )

    assert states["task", paused_task.id]["state"] == "inactive"
    assert states["host_job", disabled_job.id]["state"] == "inactive"


@pytest.mark.asyncio
async def test_paused_schedule_with_no_next_action_is_reported_as_paused(monkeypatch):
    client = _TemporalClient(
        schedule_description=_ScheduleDescription(
            info=_ScheduleInfo(next_action_times=[]),
            schedule=_Schedule(state=_ScheduleState(paused=True)),
        )
    )
    monkeypatch.setattr(temporal_status.Client, "connect", AsyncMock(return_value=client))

    states = await temporal_status.get_temporal_orchestration_states(
        [_recurring_task()], [], "unused", "unused"
    )

    state = states["task", "task-1"]
    assert state["state"] == "paused"
    assert state["next_run"] is None


@pytest.mark.asyncio
async def test_completed_workflow_has_no_next_run(monkeypatch):
    job = HostJob(
        id="completed-job",
        name="completed",
        command="echo completed",
        schedule_type="once",
        schedule_value="2026-01-01T09:00:00+00:00",
        created_by="admin",
    )
    client = _TemporalClient(
        workflow_description=_WorkflowDescription(
            execution_time=datetime.now(UTC) - timedelta(minutes=1),
            status=WorkflowExecutionStatus.COMPLETED,
        )
    )
    monkeypatch.setattr(temporal_status.Client, "connect", AsyncMock(return_value=client))

    states = await temporal_status.get_temporal_orchestration_states([], [job], "unused", "unused")

    state = states["host_job", job.id]
    assert state["state"] == "completed"
    assert state["next_run"] is None


@pytest.mark.asyncio
async def test_unknown_workflow_status_is_explicit(monkeypatch):
    job = HostJob(
        id="unknown-job",
        name="unknown",
        command="echo unknown",
        schedule_type="once",
        schedule_value="2026-01-01T09:00:00+00:00",
        created_by="admin",
    )
    client = _TemporalClient(
        workflow_description=_WorkflowDescription(execution_time=None, status=None)
    )
    monkeypatch.setattr(temporal_status.Client, "connect", AsyncMock(return_value=client))

    states = await temporal_status.get_temporal_orchestration_states([], [job], "unused", "unused")

    assert states["host_job", job.id]["state"] == "unknown"


@pytest.mark.asyncio
async def test_missing_temporal_schedule_is_not_scheduled(monkeypatch):
    missing = RPCError("missing", RPCStatusCode.NOT_FOUND, b"")
    monkeypatch.setattr(
        temporal_status.Client,
        "connect",
        AsyncMock(return_value=_ErrorClient(missing)),
    )

    states = await temporal_status.get_temporal_orchestration_states(
        [_recurring_task()], [], "unused", "unused"
    )

    state = states["task", "task-1"]
    assert state["state"] == "not_scheduled"
    assert state["schedule_id"]
    assert state["error"] is None


@pytest.mark.asyncio
async def test_missing_temporal_workflow_is_not_scheduled(monkeypatch):
    missing = RPCError("missing", RPCStatusCode.NOT_FOUND, b"")
    job = HostJob(
        id="missing-job",
        name="missing",
        command="echo missing",
        schedule_type="once",
        schedule_value="2026-01-01T09:00:00+00:00",
        created_by="admin",
    )
    monkeypatch.setattr(
        temporal_status.Client,
        "connect",
        AsyncMock(return_value=_ErrorClient(missing)),
    )

    states = await temporal_status.get_temporal_orchestration_states([], [job], "unused", "unused")

    state = states["host_job", job.id]
    assert state["state"] == "not_scheduled"
    assert state["workflow_id"]
    assert state["error"] is None


@pytest.mark.asyncio
async def test_active_once_task_and_recurring_job_use_their_temporal_ids(monkeypatch):
    task = replace(_recurring_task(), id="once-task", schedule_type="once")
    job = HostJob(
        id="recurring-job",
        name="recurring",
        command="echo recurring",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        created_by="admin",
    )
    client = _TemporalClient(
        workflow_description=_WorkflowDescription(
            execution_time=None,
            status=WorkflowExecutionStatus.COMPLETED,
        ),
        schedule_description=_ScheduleDescription(
            info=_ScheduleInfo(next_action_times=[]),
            schedule=_Schedule(state=_ScheduleState()),
        ),
    )
    monkeypatch.setattr(temporal_status.Client, "connect", AsyncMock(return_value=client))

    states = await temporal_status.get_temporal_orchestration_states(
        [task], [job], "unused", "unused"
    )

    assert states["task", task.id]["workflow_id"]
    assert states["host_job", job.id]["schedule_id"]


@pytest.mark.asyncio
async def test_temporal_item_error_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(
        temporal_status.Client,
        "connect",
        AsyncMock(return_value=_ErrorClient(RuntimeError("describe failed"))),
    )

    states = await temporal_status.get_temporal_orchestration_states(
        [_recurring_task()], [], "unused", "unused"
    )

    state = states["task", "task-1"]
    assert state["state"] == "unavailable"
    assert state["error"] == "describe failed"
