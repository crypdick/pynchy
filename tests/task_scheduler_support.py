"""Tests for task scheduler.

Tests the scheduled task execution logic, including:
- Scheduler loop initialization and duplicate prevention
- Temporal reconciliation handoff
- Task execution with different context modes
- Next run calculation for cron, interval, and once schedules
- Error handling and logging
- Group lookup and validation
"""

from __future__ import annotations

# ruff: noqa: SIM117
import asyncio
import contextlib
import inspect
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from conftest import (
    make_container_runtime_operations,
    make_settings,
)

from pynchy.config.api import SchedulerConfig
from pynchy.host.orchestrator import task_scheduler as ts_mod
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.scheduler_deps import (
    ScheduledExecutionLifecycle,
    SchedulerRuntimeConfig,
)
from pynchy.host.orchestrator.startup_readiness import StartupReadiness
from pynchy.host.orchestrator.task_scheduler import run_scheduled_agent, start_scheduler_loop
from pynchy.host.orchestrator.threads import EnsuredThread
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)

TEMPORAL_UNAVAILABLE_MESSAGE = "temporal unavailable"
TEST_ERROR_MESSAGE = "Test error"
AGENT_FAILED_MESSAGE = "Agent failed"

_scheduler_settings: ContextVar[object | None] = ContextVar("scheduler_settings", default=None)


@contextlib.contextmanager
def _patch_settings(*, poll_interval: float = 5.0, groups_dir=None, jobs=None):
    overrides = {
        "scheduler": SchedulerConfig(poll_interval=poll_interval),
        "jobs": jobs or {},
    }
    if groups_dir is not None:
        overrides["groups_dir"] = groups_dir
    s = make_settings(**overrides)
    token = _scheduler_settings.set(s)
    try:
        yield
    finally:
        _scheduler_settings.reset(token)


def _configure_scheduler_runtime(deps, settings) -> None:
    deps._scheduler_runtime = _scheduler_runtime_from_settings(settings)


def _scheduler_runtime_from_settings(settings) -> SchedulerRuntimeConfig:
    scheduler = settings.scheduler
    return SchedulerRuntimeConfig(
        temporal_address=scheduler.temporal_address,
        temporal_namespace=scheduler.temporal_namespace,
        temporal_task_queue=scheduler.temporal_task_queue,
        reconcile_schedules=scheduler.reconcile_schedules,
        poll_interval=scheduler.poll_interval,
        timezone=settings.timezone or None,
        git_sync_interval_seconds=scheduler.git_sync_interval_seconds,
        channel_reconciliation_interval_seconds=scheduler.channel_reconciliation_interval_seconds,
        auto_deploy=scheduler.auto_deploy,
        idle_timeout=settings.idle_timeout,
        groups_dir=settings.groups_dir,
        project_root=settings.project_root,
        admin_workspace=settings.notifications.admin_workspace,
        queue_max_retries=settings.queue.max_retries,
        queue_base_retry_seconds=float(settings.queue.base_retry_seconds),
        learning_max_attempts=settings.learning.max_attempts,
        canary_enabled=settings.canary.enabled,
        canary_schedule=settings.canary.schedule,
        canary_target_profile=settings.canary.target_profile,
        canary_scenario_ids=tuple(settings.canary.scenario_ids),
        external_repo_sync_slugs=(),
        config_host_cron_jobs={},
    )


class RecordingTemporalRuntime:
    instances = []

    def __init__(self, deps, scheduler_config):
        self.deps = deps
        self.scheduler_config = scheduler_config
        self.reconcile_count = 0
        RecordingTemporalRuntime.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, _tb):
        return None

    async def reconcile_schedules(self):
        self.reconcile_count += 1


@dataclass(frozen=True)
class _ActivityInfo:
    workflow_id: str
    workflow_run_id: str
    attempt: int


@contextlib.contextmanager
def _patch_scheduler_temporal_runtime(runtime_cls=RecordingTemporalRuntime):
    runtime_cls.instances = []
    with patch(
        "pynchy.host.orchestrator.task_scheduler.TemporalSchedulerRuntime",
        new=runtime_cls,
        create=True,
    ):
        yield runtime_cls


async def _run_due_task_via_scheduler(deps, task: ScheduledTask) -> None:
    """Run the public scheduled-agent runner under the caller's patches."""
    if isinstance(ts_mod.get_task_run_logs, Mock):
        await run_scheduled_agent(task, deps)
        return

    with patch(
        "pynchy.host.orchestrator.task_scheduler.get_task_run_logs",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await run_scheduled_agent(task, deps)


async def _run_scheduler_reconcile_once(deps) -> type[RecordingTemporalRuntime]:
    """Drive the public scheduler loop through one Temporal reconciliation poll."""

    def stop_after_poll(delay):
        raise asyncio.CancelledError

    with (
        patch("pynchy.host.orchestrator.task_scheduler.asyncio.sleep", side_effect=stop_after_poll),
        _patch_scheduler_temporal_runtime() as runtime_cls,
    ):
        with contextlib.suppress(asyncio.CancelledError):
            await start_scheduler_loop(deps)
    return runtime_cls


class MockSchedulerDeps:
    """Mock implementation of SchedulerDependencies protocol."""

    def __init__(self):
        self.groups: dict[str, WorkspaceProfile] = {}
        self.queue = GroupQueue(
            10,
            make_container_runtime_operations(),
        )
        self.messages: list = []
        self.host_messages: list = []
        self.system_notices: list = []
        self.agent_runs: list = []
        self.streamed_outputs: list = []
        self.context_resets: list[tuple[str, str, str]] = []
        self.thread_creations: list[tuple[str, str]] = []
        self.thread_participants: list[tuple[str, ...]] = []
        self.existing_threads: dict[str, str] = {}
        self.last_agent_timestamp: dict[str, str] = {}
        self.thread_lookups: list[tuple[str, str]] = []
        self.reused_thread_participants: list[tuple[str, tuple[str, ...]]] = []
        self.thread_creation_supported = True
        self.scheduled_execution: ScheduledExecutionLifecycle | None = None
        self.scheduled_execution_queries: list[str] = []
        self._scheduler_runtime = _scheduler_runtime_from_settings(make_settings())
        self.startup_readiness = StartupReadiness()
        self.startup_readiness.mark_ready()
        # Configurable return value for run_agent
        self._run_agent_result: str = "success"
        # Configurable side effect for run_agent (to call on_output)
        self._run_agent_side_effect = None

    def automation_memory_dir(self, _task_id: str):
        return contextlib.nullcontext(None)

    async def save_state(self) -> None:
        return None

    async def scheduled_execution_lifecycle(
        self, task_id: str
    ) -> ScheduledExecutionLifecycle | None:
        self.scheduled_execution_queries.append(task_id)
        return self.scheduled_execution

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self.groups

    @property
    def scheduler_runtime(self) -> SchedulerRuntimeConfig:
        settings = _scheduler_settings.get()
        return (
            _scheduler_runtime_from_settings(settings)
            if settings is not None
            else self._scheduler_runtime
        )

    async def register_workspace(self, profile: WorkspaceProfile) -> None:
        self.groups[profile.jid] = profile

    async def supports_thread_creation(self, parent_jid: str) -> bool:
        del parent_jid
        return self.thread_creation_supported

    async def broadcast_to_channels(self, jid: str, event) -> None:
        self.messages.append((jid, event))

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        self.host_messages.append((chat_jid, text))

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None:
        self.system_notices.append((chat_jid, text))

    async def run_declared_canaries(self, target_profile: str, scenario_ids: tuple[str, ...]):
        del target_profile, scenario_ids
        return []

    async def run_learning_review(self, packet) -> str:
        del packet
        return "completed"

    async def reconcile_linear_work_items(self) -> int | None:
        return None

    async def process_linear_plan_review_admission(self, admission) -> bool:
        del admission
        return True

    def sync_personalization(self, project_root) -> str:
        del project_root
        return ""

    async def reset_scheduled_context(
        self,
        task: ScheduledTask,
        group: WorkspaceProfile,
        occurrence_id: str,
    ) -> None:
        self.context_resets.append((task.id, group.jid, occurrence_id))

    async def review_linear_plan(self, *_args, **_kwargs):
        raise AssertionError("plan review is outside task scheduler tests")

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str:
        self.thread_creations.append((parent_jid, name))
        self.thread_participants.append(participant_ids)
        child_jid = f"discord:channel:scheduled-{len(self.thread_creations)}"
        self.existing_threads[name] = child_jid
        return child_jid

    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        self.thread_lookups.append((parent_jid, name))
        return self.existing_threads.get(name)

    async def add_thread_participants(
        self,
        child_jid: str,
        participant_ids: tuple[str, ...],
    ) -> None:
        self.reused_thread_participants.append((child_jid, participant_ids))

    async def ensure_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> EnsuredThread:
        child_jid = await self.find_thread(parent_jid, name)
        if child_jid is not None:
            await self.add_thread_participants(child_jid, participant_ids)
            return EnsuredThread(jid=child_jid, created=False)
        return EnsuredThread(
            jid=await self.create_thread(
                parent_jid,
                name,
                participant_ids=participant_ids,
            ),
            created=True,
        )

    async def run_agent(
        self,
        group,
        chat_jid,
        messages,
        on_output=None,
        extra_system_notices=None,
        *,
        is_scheduled_task=False,
        repo_access_override=None,
        input_source="user",
        turn_id=None,
        resume_session_id=None,
        automation_memory_dir=None,
    ) -> str:
        self.agent_runs.append(
            {
                "group": group,
                "chat_jid": chat_jid,
                "messages": messages,
                "on_output": on_output,
                "extra_system_notices": extra_system_notices,
                "is_scheduled_task": is_scheduled_task,
                "repo_access_override": repo_access_override,
                "input_source": input_source,
                "turn_id": turn_id,
                "resume_session_id": resume_session_id,
                "automation_memory_dir": automation_memory_dir,
            }
        )
        if self._run_agent_side_effect:
            resume_kwargs = (
                {"resume_session_id": resume_session_id} if resume_session_id is not None else {}
            )
            result = self._run_agent_side_effect(
                group,
                chat_jid,
                messages,
                on_output,
                is_scheduled_task=is_scheduled_task,
                repo_access_override=repo_access_override,
                input_source=input_source,
                turn_id=turn_id,
                **resume_kwargs,
            )
            if inspect.isawaitable(result):
                return await result
            return result
        return self._run_agent_result

    async def handle_streamed_output(self, chat_jid, group, result, *, turn_id=None) -> bool:
        self.streamed_outputs.append((chat_jid, group, result, turn_id))
        return bool(result.result)


@pytest.fixture
def mock_deps():
    """Create mock scheduler dependencies."""
    return MockSchedulerDeps()


@pytest.fixture
def sample_task():
    """Create a sample scheduled task."""
    return ScheduledTask(
        id="task-1",
        group_folder="test-group",
        chat_jid="test@g.us",
        prompt="Test task",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        session_policy=SessionPolicy.CONTINUE,
        bound_chat_jid="test@g.us",
        bound_group_folder="test-group",
        next_run=datetime.now(UTC).isoformat(),
        status="active",
    )


@pytest.fixture
def sample_group():
    """Create a sample registered group."""
    return WorkspaceProfile(
        jid="test@g.us",
        name="Test Group",
        folder="test-group",
        trigger="@bot",
        added_at=datetime.now(UTC).isoformat(),
    )
