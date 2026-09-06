"""Public scheduler reconciliation behavior at Temporal control-plane edges."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from temporalio.client import ScheduleAlreadyRunningError
from temporalio.service import RPCError, RPCStatusCode

from pynchy.config.api import SchedulerConfig
from pynchy.host.orchestrator.temporal import scheduler as temporal_scheduler
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
    agent_task_occurrence_workflow_id,
)
from tests.temporal_scheduler_support import (
    FakeScheduleClient,
    FakeScheduleHandle,
    NullSchedulerDeps,
    _scheduler_runtime,
    _WorkflowListEntry,
)


def _once_task(
    task_id: str,
    *,
    due_at: str | None = None,
    status: str = "active",
    superseded: bool = False,
) -> ScheduledTask:
    due_at = due_at or (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    return ScheduledTask(
        id=task_id,
        group_folder="project",
        chat_jid="discord:channel:project",
        prompt="run this",
        schedule_type="once",
        schedule_value=due_at,
        session_policy=SessionPolicy.CONTINUE,
        status=status,  # type: ignore[arg-type]
        occurrence_due_at=due_at,
        superseded_occurrence_due_at=due_at if superseded else None,
        superseded_occurrence_generation=0 if superseded else None,
    )


class _DeleteErrorHandle(FakeScheduleHandle):
    async def delete(self):
        raise RPCError("missing", RPCStatusCode.NOT_FOUND, b"")


class _DeleteErrorClient(FakeScheduleClient):
    def get_schedule_handle(self, schedule_id):
        return self.handles.setdefault(schedule_id, _DeleteErrorHandle(schedule_id))


class _DeleteFailureHandle(FakeScheduleHandle):
    async def delete(self):
        raise RPCError("unavailable", RPCStatusCode.INTERNAL, b"")


class _DeleteFailureClient(FakeScheduleClient):
    def get_schedule_handle(self, schedule_id):
        return self.handles.setdefault(schedule_id, _DeleteFailureHandle(schedule_id))


class _ScheduleErrorClient(FakeScheduleClient):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.failed = False

    async def create_schedule(self, schedule_id, schedule, **kwargs):
        if not self.failed:
            self.failed = True
            raise self.error
        return await super().create_schedule(schedule_id, schedule, **kwargs)


async def test_invalid_recurring_definition_leaves_temporal_unchanged(monkeypatch):
    task = _once_task("invalid")
    task.schedule_type = "interval"
    task.schedule_value = "not an interval"
    client = FakeScheduleClient()
    runtime = temporal_scheduler.TemporalSchedulerRuntime(
        deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime()
    )
    runtime.client = client
    monkeypatch.setattr(
        temporal_scheduler, "get_all_tasks", lambda: asyncio.sleep(0, result=[task])
    )
    monkeypatch.setattr(
        temporal_scheduler, "get_all_host_jobs", lambda: asyncio.sleep(0, result=[])
    )

    with pytest.raises(ValueError, match="invalid literal"):
        await runtime.reconcile_schedules()

    assert client.created_schedules == []
    assert client.started_workflows == []
    assert client.handles == {}
    assert client.workflow_handles == {}


@pytest.mark.asyncio
async def test_reconcile_requires_started_temporal_runtime() -> None:
    runtime = temporal_scheduler.TemporalSchedulerRuntime(
        deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
    )

    with pytest.raises(RuntimeError, match="Temporal scheduler runtime has not been started"):
        await runtime.reconcile_schedules()


@pytest.mark.asyncio
async def test_reconcile_skips_paused_tasks_and_updates_existing_schedule() -> None:
    client = _ScheduleErrorClient(ScheduleAlreadyRunningError())
    runtime = temporal_scheduler.TemporalSchedulerRuntime(
        deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
    )
    runtime.client = client
    paused = _once_task("paused", status="paused")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        temporal_scheduler, "get_all_tasks", lambda: asyncio.sleep(0, result=[paused])
    )
    monkeypatch.setattr(
        temporal_scheduler, "get_all_host_jobs", lambda: asyncio.sleep(0, result=[])
    )
    try:
        await runtime.reconcile_schedules()
    finally:
        monkeypatch.undo()

    assert client.started_workflows == []
    assert client.handles["pynchy-git-sync-host"].updates


@pytest.mark.asyncio
async def test_reconcile_updates_schedule_for_temporal_already_exists() -> None:
    client = _ScheduleErrorClient(RPCError("exists", RPCStatusCode.ALREADY_EXISTS, b""))
    runtime = temporal_scheduler.TemporalSchedulerRuntime(
        deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
    )
    runtime.client = client
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(temporal_scheduler, "get_all_tasks", lambda: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(
        temporal_scheduler, "get_all_host_jobs", lambda: asyncio.sleep(0, result=[])
    )
    try:
        await runtime.reconcile_schedules()
    finally:
        monkeypatch.undo()

    assert client.handles["pynchy-git-sync-host"].updates


@pytest.mark.asyncio
async def test_reconcile_cleans_orphaned_workflows_and_stale_schedules() -> None:
    due_at = datetime.now(UTC) + timedelta(minutes=5)
    orphan_id = "pynchy-agent-task-orphan"
    client = _DeleteErrorClient()
    client.workflow_executions = [
        _WorkflowListEntry(orphan_id, "orphan-run", "ScheduledAgentTaskWorkflow", due_at),
        _WorkflowListEntry("foreign", "foreign-run", "InteractiveMessageWorkflow", due_at),
        _WorkflowListEntry(
            "pynchy-agent-task-due",
            "due-run",
            "ScheduledAgentTaskWorkflow",
            datetime.now(UTC),
        ),
        _WorkflowListEntry("pynchy-unknown", "unknown-run", "OtherWorkflow", due_at),
    ]
    client.get_workflow_handle(orphan_id, run_id="orphan-run").cancel_error = RPCError(
        "missing", RPCStatusCode.NOT_FOUND, b""
    )
    client.schedule_ids = [
        "foreign-schedule",
        "pynchy-git-sync-host",
        "pynchy-agent-schedule-stale",
    ]
    runtime = temporal_scheduler.TemporalSchedulerRuntime(
        deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
    )
    runtime.client = client
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(temporal_scheduler, "get_all_tasks", lambda: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(
        temporal_scheduler, "get_all_host_jobs", lambda: asyncio.sleep(0, result=[])
    )
    try:
        await runtime.reconcile_schedules()
    finally:
        monkeypatch.undo()

    assert client.workflow_handles[orphan_id, "orphan-run"].cancelled is False
    assert client.handles["pynchy-agent-schedule-stale"].deleted is False


@pytest.mark.asyncio
async def test_reconcile_propagates_orphan_cancellation_failure() -> None:
    due_at = datetime.now(UTC) + timedelta(minutes=5)
    workflow_id = "pynchy-agent-task-orphan"
    client = FakeScheduleClient()
    client.workflow_executions = [
        _WorkflowListEntry(workflow_id, "orphan-run", "ScheduledAgentTaskWorkflow", due_at)
    ]
    client.get_workflow_handle(workflow_id, run_id="orphan-run").cancel_error = RPCError(
        "unavailable", RPCStatusCode.INTERNAL, b""
    )
    runtime = temporal_scheduler.TemporalSchedulerRuntime(
        deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
    )
    runtime.client = client
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(temporal_scheduler, "get_all_tasks", lambda: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(
        temporal_scheduler, "get_all_host_jobs", lambda: asyncio.sleep(0, result=[])
    )
    try:
        with pytest.raises(RPCError, match="unavailable"):
            await runtime.reconcile_schedules()
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_reconcile_defers_one_resumed_owner_and_ignores_ambiguous_ones() -> None:
    due_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    solo = _once_task("solo", due_at=due_at, superseded=True)
    ambiguous_a = _once_task("ambiguous/a", due_at=due_at, superseded=True)
    ambiguous_b = _once_task("ambiguous-a", due_at=due_at, superseded=True)
    solo_workflow_id = agent_task_occurrence_workflow_id("solo", due_at, 0)
    ambiguous_workflow_id = agent_task_occurrence_workflow_id("ambiguous/a", due_at, 0)
    client = FakeScheduleClient()
    client.workflow_executions = [
        _WorkflowListEntry("wrong-type", "wrong-run", "OtherWorkflow", None),
        _WorkflowListEntry("unowned", "unowned-run", "ScheduledAgentTaskWorkflow", None),
        _WorkflowListEntry(
            solo_workflow_id,
            "solo-run",
            "ScheduledAgentTaskWorkflow",
            datetime.fromisoformat(due_at),
        ),
        _WorkflowListEntry(
            ambiguous_workflow_id,
            "ambiguous-run",
            "ScheduledAgentTaskWorkflow",
            datetime.fromisoformat(due_at),
        ),
    ]
    client.get_workflow_handle(solo_workflow_id, run_id="solo-run").cancel_error = RPCError(
        "missing", RPCStatusCode.NOT_FOUND, b""
    )
    runtime = temporal_scheduler.TemporalSchedulerRuntime(
        deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
    )
    runtime.client = client
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        temporal_scheduler,
        "get_all_tasks",
        lambda: asyncio.sleep(0, result=[solo, ambiguous_a, ambiguous_b]),
    )
    monkeypatch.setattr(
        temporal_scheduler, "get_all_host_jobs", lambda: asyncio.sleep(0, result=[])
    )
    try:
        await runtime.reconcile_schedules()
    finally:
        monkeypatch.undo()

    assert client.workflow_handles[solo_workflow_id, "solo-run"].cancelled is False


@pytest.mark.asyncio
async def test_reconcile_propagates_resumed_workflow_cancellation_failure() -> None:
    due_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    task = _once_task("solo", due_at=due_at, superseded=True)
    workflow_id = agent_task_occurrence_workflow_id("solo", due_at, 0)
    client = FakeScheduleClient()
    client.workflow_executions = [
        _WorkflowListEntry(
            workflow_id,
            "solo-run",
            "ScheduledAgentTaskWorkflow",
            datetime.fromisoformat(due_at),
        )
    ]
    client.get_workflow_handle(workflow_id, run_id="solo-run").cancel_error = RPCError(
        "unavailable", RPCStatusCode.INTERNAL, b""
    )
    runtime = temporal_scheduler.TemporalSchedulerRuntime(
        deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
    )
    runtime.client = client
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        temporal_scheduler, "get_all_tasks", lambda: asyncio.sleep(0, result=[task])
    )
    monkeypatch.setattr(
        temporal_scheduler, "get_all_host_jobs", lambda: asyncio.sleep(0, result=[])
    )
    try:
        with pytest.raises(RPCError, match="unavailable"):
            await runtime.reconcile_schedules()
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_reconcile_propagates_stale_schedule_deletion_failure() -> None:
    client = _DeleteFailureClient()
    client.schedule_ids = ["pynchy-agent-schedule-stale"]
    runtime = temporal_scheduler.TemporalSchedulerRuntime(
        deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
    )
    runtime.client = client
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(temporal_scheduler, "get_all_tasks", lambda: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(
        temporal_scheduler, "get_all_host_jobs", lambda: asyncio.sleep(0, result=[])
    )
    try:
        with pytest.raises(RPCError, match="unavailable"):
            await runtime.reconcile_schedules()
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_reconcile_propagates_unexpected_temporal_errors() -> None:
    error = RPCError("unavailable", RPCStatusCode.INTERNAL, b"")
    client = _ScheduleErrorClient(error)
    runtime = temporal_scheduler.TemporalSchedulerRuntime(
        deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
    )
    runtime.client = client
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(temporal_scheduler, "get_all_tasks", lambda: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(
        temporal_scheduler, "get_all_host_jobs", lambda: asyncio.sleep(0, result=[])
    )
    try:
        with pytest.raises(RPCError, match="unavailable"):
            await runtime.reconcile_schedules()
    finally:
        monkeypatch.undo()
