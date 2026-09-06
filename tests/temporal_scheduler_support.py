"""Tests for the Temporal scheduler control-plane integration."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import make_container_runtime_operations
from temporalio.exceptions import WorkflowAlreadyStartedError

from pynchy.config.api import (
    CanaryConfig,
    JobConfig,
    SchedulerConfig,
)
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.scheduler_deps import (
    ConfigHostCronJob,
    SchedulerRuntimeConfig,
)
from pynchy.host.orchestrator.startup_readiness import (
    StartupReadiness,
)
from pynchy.learning_packets import LearningPacket
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)

if TYPE_CHECKING:
    from pynchy.canary_contracts import (
        CanaryRun,
    )
    from pynchy.linear_plan_types import LinearPlanReviewAdmission
    from pynchy.workspace.api import WorkspaceProfile

TEMPORAL_UNAVAILABLE_MESSAGE = "temporal unavailable"
PAUSED_TASK_RUN_MESSAGE = "paused tasks must not run"


def _scheduler_runtime(
    scheduler: SchedulerConfig | None = None,
    *,
    timezone: str | None = "UTC",
    jobs: dict[str, JobConfig] | None = None,
    canary: CanaryConfig | None = None,
    project_root: Path = Path("/project"),
    external_repo_sync_slugs: tuple[str, ...] = (),
) -> SchedulerRuntimeConfig:
    scheduler = scheduler or SchedulerConfig()
    canary = canary or CanaryConfig()
    config_host_cron_jobs = {
        name: ConfigHostCronJob(
            command=job.command or "",
            schedule=job.schedule or "",
            cwd=job.cwd,
            timeout_seconds=job.timeout_seconds,
            quiet_on_success=job.quiet_on_success is True,
            memory_enabled=job.memory,
        )
        for name, job in (jobs or {}).items()
        if job.is_host and job.enabled
    }
    return SchedulerRuntimeConfig(
        temporal_address=scheduler.temporal_address,
        temporal_namespace=scheduler.temporal_namespace,
        temporal_task_queue=scheduler.temporal_task_queue,
        reconcile_schedules=scheduler.reconcile_schedules,
        poll_interval=scheduler.poll_interval,
        timezone=timezone,
        git_sync_interval_seconds=scheduler.git_sync_interval_seconds,
        channel_reconciliation_interval_seconds=scheduler.channel_reconciliation_interval_seconds,
        auto_deploy=scheduler.auto_deploy,
        idle_timeout=300.0,
        groups_dir=Path("groups"),
        project_root=project_root,
        admin_workspace=None,
        queue_max_retries=5,
        queue_base_retry_seconds=5.0,
        learning_max_attempts=3,
        canary_enabled=canary.enabled,
        canary_schedule=canary.schedule,
        canary_target_profile=canary.target_profile,
        canary_scenario_ids=tuple(canary.scenario_ids),
        external_repo_sync_slugs=external_repo_sync_slugs,
        config_host_cron_jobs=config_host_cron_jobs,
    )


def _ready_startup() -> StartupReadiness:
    readiness = StartupReadiness()
    readiness.mark_ready()
    return readiness


@dataclass
class _RuntimePaths:
    project_root: Path = Path("/project")
    data_dir: Path = Path("/data")


@dataclass
class NullSchedulerDeps:
    """Structural fake for SchedulerDependencies."""

    queue: GroupQueue = field(
        default_factory=lambda: GroupQueue(
            10,
            make_container_runtime_operations(),
        )
    )
    groups: dict[str, WorkspaceProfile] = field(default_factory=dict)
    last_agent_timestamp: dict[str, str] = field(default_factory=dict)
    startup_readiness: StartupReadiness = field(default_factory=_ready_startup)
    agent_execution_runtime: _RuntimePaths = field(default_factory=_RuntimePaths)
    scheduler_runtime: SchedulerRuntimeConfig = field(default_factory=_scheduler_runtime)

    @property
    def workspaces(self):
        return self.groups

    def automation_memory_dir(self, _task_id: str):
        return nullcontext(None)

    async def broadcast_to_channels(self, jid, event) -> None: ...

    async def broadcast_host_message(self, chat_jid, text) -> None: ...

    async def broadcast_system_notice(self, chat_jid, text) -> None: ...

    async def run_declared_canaries(self, target_profile, scenario_ids) -> list[CanaryRun]:
        return []

    async def run_learning_review(self, packet) -> str:
        del packet
        return "completed"

    async def reconcile_linear_work_items(self) -> int | None:
        return None

    async def process_linear_plan_review_admission(
        self,
        admission: LinearPlanReviewAdmission,
    ) -> bool:
        raise AssertionError(f"Unexpected plan review admission for {admission}")

    async def reset_scheduled_context(self, task, group, occurrence_id) -> None: ...

    async def save_state(self) -> None: ...

    async def review_linear_plan(self, request):
        raise AssertionError(f"Unexpected plan review for {request}")

    async def run_agent(self, *args, **kwargs) -> str:
        return "success"

    async def handle_streamed_output(self, chat_jid, group, result) -> bool:
        return False


@dataclass(frozen=True)
class _ScheduleDescription:
    """The Temporal schedule-update subset the reconciler reads."""

    description: object | None


@dataclass(frozen=True)
class _ScheduleListEntry:
    """The Temporal schedule-list subset the reconciler reads."""

    id: str


@dataclass(frozen=True)
class _WorkflowListEntry:
    """The Temporal workflow-list subset the reconciler reads."""

    id: str
    run_id: str
    workflow_type: str
    execution_time: datetime | None


class FakeScheduleHandle:
    def __init__(self, schedule_id: str):
        self.schedule_id = schedule_id
        self.updates = []
        self.deleted = False

    async def update(self, updater):
        update = updater(_ScheduleDescription(description=None))
        self.updates.append(update)

    async def delete(self):
        self.deleted = True


class FakeWorkflowHandle:
    def __init__(self, workflow_id: str, run_id: str):
        self.workflow_id = workflow_id
        self.run_id = run_id
        self.cancelled = False
        self.cancel_error = None

    async def cancel(self):
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled = True


class FakeScheduleClient:
    def __init__(self):
        self.created_schedules = []
        self.started_workflows = []
        self.handles = {}
        self.schedule_ids = []
        self.workflow_executions = []
        self.workflow_handles = {}
        self.workflow_query = None

    async def create_schedule(self, schedule_id, schedule, **kwargs):
        self.created_schedules.append((schedule_id, schedule, kwargs))
        return self.handles.setdefault(schedule_id, FakeScheduleHandle(schedule_id))

    def get_schedule_handle(self, schedule_id):
        return self.handles.setdefault(schedule_id, FakeScheduleHandle(schedule_id))

    async def list_schedules(self):
        return _ScheduleIterator(self.schedule_ids)

    def list_workflows(self, query):
        self.workflow_query = query
        return _WorkflowIterator(self.workflow_executions)

    def get_workflow_handle(self, workflow_id, *, run_id=None):
        assert run_id is not None
        key = (workflow_id, run_id)
        return self.workflow_handles.setdefault(key, FakeWorkflowHandle(*key))

    async def start_workflow(self, workflow, *posargs, **kwargs):
        assert len(posargs) <= 1
        workflow_args = tuple(kwargs.pop("args", ()))
        assert not (posargs and workflow_args)
        self.started_workflows.append((workflow, posargs or workflow_args, kwargs))


class DeduplicatingFakeScheduleClient(FakeScheduleClient):
    """Model Temporal's workflow-ID idempotency across reconciliations."""

    def __init__(self):
        super().__init__()
        self.workflow_ids = set()

    async def start_workflow(self, workflow, *posargs, **kwargs):
        workflow_id = kwargs["id"]
        if workflow_id in self.workflow_ids:
            raise WorkflowAlreadyStartedError(workflow_id, workflow.__qualname__)
        self.workflow_ids.add(workflow_id)
        await super().start_workflow(workflow, *posargs, **kwargs)


class _ScheduleIterator:
    def __init__(self, schedule_ids):
        self._schedule_ids = iter(schedule_ids)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        try:
            schedule_id = next(self._schedule_ids)
        except StopIteration:
            raise StopAsyncIteration from None
        return _ScheduleListEntry(id=schedule_id)


class _WorkflowIterator:
    def __init__(self, executions):
        self._executions = iter(executions)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        try:
            return next(self._executions)
        except StopIteration:
            raise StopAsyncIteration from None


@pytest.fixture
def learning_packet() -> LearningPacket:
    return LearningPacket(
        job_id="learning job one",
        chat_jid="slack:C123",
        group_folder="research",
        profile="Deep Work",
        created_at="2026-07-07T10:00:00+00:00",
        messages=[{"role": "user", "content": "remember this workflow"}],
        final_answer="Done.",
        tool_counts={"Bash": 1},
        error_snippets=[],
        loaded_skills=[],
        provenance={"run_id": "run-123"},
    )


@pytest.fixture
def temporal_task() -> ScheduledTask:
    return ScheduledTask(
        id="task/with spaces",
        group_folder="test-group",
        chat_jid="test@g.us",
        prompt="Test task",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        session_policy=SessionPolicy.RESET_BEFORE_RUN,
        bound_chat_jid="test@g.us",
        bound_group_folder="test-group",
        next_run=datetime(2026, 7, 7, 9, 0, tzinfo=UTC).isoformat(),
        status="active",
    )
