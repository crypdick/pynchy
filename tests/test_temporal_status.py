"""Tests for Temporal-backed scheduled-work status."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings
from temporalio.client import WorkflowExecutionStatus

from pynchy.config.scheduler_models import SchedulerConfig
from pynchy.host.orchestrator.temporal import status as temporal_status
from pynchy.types import HostJob, ScheduledTask, SessionPolicy


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


def _settings():
    return make_settings(
        scheduler=SchedulerConfig(
            temporal_address="temporal.example:7233",
            temporal_namespace="pynchy-test",
        )
    )


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
    monkeypatch.setattr(temporal_status, "get_settings", _settings)
    monkeypatch.setattr(temporal_status.Client, "connect", AsyncMock(return_value=client))

    states = await temporal_status.get_temporal_orchestration_states([_recurring_task()], [])

    state = states["task", "task-1"]
    assert state["source"] == "temporal"
    assert state["state"] == "scheduled"
    assert state["next_run"] == temporal_next_run.isoformat()


@pytest.mark.asyncio
async def test_unavailable_temporal_has_no_sqlite_next_run_fallback(monkeypatch):
    monkeypatch.setattr(temporal_status, "get_settings", _settings)
    monkeypatch.setattr(
        temporal_status.Client,
        "connect",
        AsyncMock(side_effect=RuntimeError("connection refused")),
    )

    states = await temporal_status.get_temporal_orchestration_states([_recurring_task()], [])

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
    monkeypatch.setattr(temporal_status, "get_settings", _settings)
    monkeypatch.setattr(temporal_status.Client, "connect", AsyncMock(return_value=client))

    states = await temporal_status.get_temporal_orchestration_states([], [job])

    state = states["host_job", "host-job-1"]
    assert state["state"] == "delayed"
    assert state["next_run"] == due_at.isoformat()
