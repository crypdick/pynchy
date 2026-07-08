"""Temporal runtime for scheduled Pynchy work.

Temporal owns durable execution; activities delegate to the existing host
runner so container IPC and streaming behavior stay in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from datetime import timedelta
from typing import Any
from uuid import uuid4

from temporalio import activity
from temporalio.client import (
    Client,
    Schedule,
    ScheduleAlreadyRunningError,
    ScheduleUpdate,
)
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from pynchy.config import get_settings
from pynchy.config.models import SchedulerConfig
from pynchy.host.learning.packet_codec import packet_from_payload, packet_to_payload
from pynchy.host.learning.packet_models import LearningPacket
from pynchy.host.learning.review_runner import run_learning_review as _run_learning_review_agent
from pynchy.host.orchestrator.task_scheduler import (
    SchedulerDependencies,
    _run_scheduled_agent,
)
from pynchy.host.orchestrator.temporal.host_jobs import (
    run_config_host_cron_job,
    run_database_host_job,
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
    _update_temporal_scheduler_status,
    _utc_timestamp,
    bind_scheduler_deps,
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    get_temporal_scheduler_status as _get_temporal_scheduler_status,
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    reset_temporal_scheduler_status as _reset_temporal_scheduler_status,
)
from pynchy.host.orchestrator.temporal.schedules import (
    SCHEDULE_PREFIXES,
    agent_task_schedule_id,
    agent_task_workflow_id,
    config_host_cron_schedule_id,
    database_host_job_schedule_id,
    database_host_job_workflow_id,
    once_due_at,
    safe_workflow_fragment,
    schedule_for_agent_task,
    schedule_for_config_host_cron,
    schedule_for_database_host_job,
    start_delay_until,
)
from pynchy.host.orchestrator.temporal.workflows import (
    ConfigHostCronWorkflow,
    DatabaseHostJobWorkflow,
    LearningReviewWorkflow,
    ScheduledAgentTaskWorkflow,
)
from pynchy.logger import logger
from pynchy.state import (
    get_all_host_jobs,
    get_all_tasks,
    get_task_by_id,
)
from pynchy.types import HostJob, ScheduledTask

_active_runtime: TemporalSchedulerRuntime | None = None
_WORKFLOW_MODULE = "pynchy.host.orchestrator.temporal.workflows"


def reset_temporal_scheduler_status() -> None:
    """Clear the in-process Temporal worker status snapshot."""
    _reset_temporal_scheduler_status()


def get_temporal_scheduler_status() -> dict[str, Any]:
    """Return the in-process Temporal worker status snapshot."""
    return _get_temporal_scheduler_status()


def learning_review_workflow_id(packet: LearningPacket) -> str:
    """Return the idempotency key for one hidden learning review."""
    return f"pynchy-learning-review-{safe_workflow_fragment(packet.job_id)}"


async def start_learning_review_workflow(packet: LearningPacket) -> None:
    """Start a Temporal learning review workflow using the active runtime."""
    if _active_runtime is None:
        raise RuntimeError("Temporal scheduler runtime has not been started")
    await _active_runtime.start_learning_review(packet)


def scheduler_workflow_runner() -> WorkflowRunner:
    """Return the Temporal sandbox runner for Pynchy scheduler workflows."""
    # Temporal's sandbox re-imports workflow modules. Pynchy's package import
    # installs beartype import hooks, which are host-process instrumentation
    # rather than workflow logic. Pass through only the deterministic workflow
    # definition module so the sandbox does not re-run that package import path.
    restrictions = SandboxRestrictions.default.with_passthrough_modules(_WORKFLOW_MODULE)
    return SandboxedWorkflowRunner(restrictions=restrictions)


@activity.defn(name="run_scheduled_agent_task")
async def run_scheduled_agent_task(task_id: str) -> str:
    """Temporal activity that runs one active scheduled agent task."""
    task = await get_task_by_id(task_id)
    if task is None or task.status != "active":
        logger.info("Temporal scheduled task skipped", task_id=task_id)
        _record_activity_result(task_id, "skipped")
        return "skipped"

    try:
        await _run_scheduled_agent(task, _require_scheduler_deps())
    except Exception as exc:  # allow: exception-handling - record activity failure
        _record_activity_result(task_id, "error", str(exc))
        raise
    _record_activity_result(task_id, "completed")
    return "completed"


@activity.defn(name="run_learning_review")
async def run_learning_review(packet_payload: dict[str, Any]) -> str:
    """Temporal activity that runs one hidden Obsidian learning review."""
    packet = packet_from_payload(packet_payload)
    try:
        result = await _run_learning_review(packet, _require_scheduler_deps())
    except Exception as exc:  # allow: exception-handling - record activity failure
        _record_activity_result(packet.job_id, "error", str(exc))
        raise
    _record_activity_result(packet.job_id, result)
    return result


async def _run_learning_review(packet: LearningPacket, deps: SchedulerDependencies) -> str:
    async def run_agent_via_queue(*args: Any, **kwargs: Any) -> str:
        return await _run_agent_via_queue(deps, *args, **kwargs)

    return await _run_learning_review_agent(packet, run_agent_via_queue)


async def _run_agent_via_queue(
    deps: SchedulerDependencies,
    *args: Any,
    **kwargs: Any,
) -> str:
    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[str] = loop.create_future()
    group_jid = _learning_run_group_jid(args, kwargs)

    async def run_queued_agent() -> None:
        if result_future.cancelled():
            return
        try:
            result = await deps.run_agent(*args, **kwargs)
        except asyncio.CancelledError:
            if not result_future.done():
                result_future.cancel()
            raise
        except Exception as exc:  # allow: exception-handling - propagate queued run failure
            if not result_future.done():
                result_future.set_exception(exc)
        else:
            if not result_future.done():
                result_future.set_result(result)

    accepted = deps.queue.enqueue_task(
        group_jid,
        f"learning-review-{uuid4().hex}",
        run_queued_agent,
    )
    if accepted is False:
        result_future.cancel()
        raise asyncio.CancelledError()

    try:
        return await result_future
    except asyncio.CancelledError:
        result_future.cancel()
        raise


def _learning_run_group_jid(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    if len(args) >= 2 and isinstance(args[1], str):
        return args[1]

    chat_jid = kwargs.get("chat_jid")
    if isinstance(chat_jid, str):
        return chat_jid

    raise TypeError("learning reviewer run requires a chat_jid")


class TemporalSchedulerRuntime:
    """Owns the Temporal client, worker, and schedule reconciliation."""

    def __init__(self, deps: SchedulerDependencies, scheduler_config: SchedulerConfig) -> None:
        self.deps = deps
        self.scheduler_config = scheduler_config
        self.client: Client | None = None
        self._worker: Worker | None = None
        self._worker_stack = contextlib.AsyncExitStack()

    async def __aenter__(self) -> TemporalSchedulerRuntime:
        global _active_runtime
        bind_scheduler_deps(self.deps)
        try:
            self.client = await Client.connect(
                self.scheduler_config.temporal_address,
                namespace=self.scheduler_config.temporal_namespace,
            )
            self._worker = Worker(
                self.client,
                task_queue=self.scheduler_config.temporal_task_queue,
                workflows=[
                    ScheduledAgentTaskWorkflow,
                    DatabaseHostJobWorkflow,
                    ConfigHostCronWorkflow,
                    LearningReviewWorkflow,
                ],
                activities=[
                    run_scheduled_agent_task,
                    run_database_host_job,
                    run_config_host_cron_job,
                    run_learning_review,
                ],
                workflow_runner=scheduler_workflow_runner(),
            )
            await self._worker_stack.enter_async_context(self._worker)
        except BaseException as exc:  # allow: exception-handling - startup cleanup then re-raise
            await self._worker_stack.aclose()
            bind_scheduler_deps(None)
            _update_temporal_scheduler_status(worker_running=False, last_error=str(exc))
            raise
        _active_runtime = self
        _update_temporal_scheduler_status(worker_running=True, last_error=None)
        logger.info(
            "Temporal scheduler runtime started",
            address=self.scheduler_config.temporal_address,
            namespace=self.scheduler_config.temporal_namespace,
            task_queue=self.scheduler_config.temporal_task_queue,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        global _active_runtime
        await self._worker_stack.aclose()
        bind_scheduler_deps(None)
        if _active_runtime is self:
            _active_runtime = None
        _update_temporal_scheduler_status(worker_running=False)

    async def start_scheduled_agent_task(self, task: ScheduledTask) -> None:
        """Start a Temporal workflow for the due task if one is not already running."""
        if self.client is None:
            raise RuntimeError("Temporal scheduler runtime has not been started")

        workflow_id = agent_task_workflow_id(task)
        await self._start_workflow(
            ScheduledAgentTaskWorkflow.run,
            task.id,
            workflow_id=workflow_id,
            status_id=task.id,
        )

    async def start_learning_review(self, packet: LearningPacket) -> None:
        """Start a Temporal workflow for one hidden learning review packet."""
        if self.client is None:
            raise RuntimeError("Temporal scheduler runtime has not been started")

        await self._start_workflow(
            LearningReviewWorkflow.run,
            packet_to_payload(packet),
            get_settings().learning.max_attempts,
            workflow_id=learning_review_workflow_id(packet),
            status_id=packet.job_id,
        )

    async def reconcile_schedules(self) -> None:
        """Reconcile Pynchy's desired scheduled work into Temporal schedules."""
        if self.client is None:
            raise RuntimeError("Temporal scheduler runtime has not been started")

        desired_schedule_ids: set[str] = set()
        settings = get_settings()
        tasks = await get_all_tasks()
        host_jobs = await get_all_host_jobs()

        for task in tasks:
            if task.status != "active":
                continue
            if task.schedule_type == "once":
                await self._start_once_agent_task(task)
                continue
            schedule_id = agent_task_schedule_id(task)
            desired_schedule_ids.add(schedule_id)
            await self._upsert_schedule(schedule_id, schedule_for_agent_task(task))

        for job in host_jobs:
            if job.status != "active" or not job.enabled:
                continue
            if job.schedule_type == "once":
                await self._start_once_database_host_job(job)
                continue
            schedule_id = database_host_job_schedule_id(job)
            desired_schedule_ids.add(schedule_id)
            await self._upsert_schedule(schedule_id, schedule_for_database_host_job(job))

        for job_name, cron_job in settings.cron_jobs.items():
            if not cron_job.enabled:
                continue
            schedule_id = config_host_cron_schedule_id(job_name)
            desired_schedule_ids.add(schedule_id)
            await self._upsert_schedule(
                schedule_id,
                schedule_for_config_host_cron(job_name, cron_job.schedule),
            )

        await self._delete_stale_schedules(desired_schedule_ids)

    async def _start_once_agent_task(self, task: ScheduledTask) -> None:
        workflow_id = agent_task_workflow_id(task)
        await self._start_workflow(
            ScheduledAgentTaskWorkflow.run,
            task.id,
            workflow_id=workflow_id,
            status_id=task.id,
            start_delay=start_delay_until(once_due_at(task.next_run or task.schedule_value)),
        )

    async def _start_once_database_host_job(self, job: HostJob) -> None:
        workflow_id = database_host_job_workflow_id(job)
        await self._start_workflow(
            DatabaseHostJobWorkflow.run,
            job.id,
            workflow_id=workflow_id,
            status_id=job.id,
            start_delay=start_delay_until(once_due_at(job.next_run or job.schedule_value)),
        )

    async def _start_workflow(
        self,
        workflow,
        *args: Any,
        workflow_id: str,
        status_id: str,
        start_delay: timedelta | None = None,
    ) -> None:
        if self.client is None:
            raise RuntimeError("Temporal scheduler runtime has not been started")

        try:
            if start_delay is None:
                await self.client.start_workflow(
                    workflow,
                    *args,
                    id=workflow_id,
                    task_queue=self.scheduler_config.temporal_task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            else:
                await self.client.start_workflow(
                    workflow,
                    *args,
                    id=workflow_id,
                    task_queue=self.scheduler_config.temporal_task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    start_delay=start_delay,
                )
        except WorkflowAlreadyStartedError:
            _update_temporal_scheduler_status(
                last_workflow_id=workflow_id,
                last_task_id=status_id,
                last_result="already_started",
                last_started_at=_utc_timestamp(),
                last_completed_at=None,
                last_error=None,
            )
            logger.debug(
                "Temporal scheduled workflow already started",
                work_id=status_id,
                workflow_id=workflow_id,
            )
            return
        except Exception as exc:  # allow: exception-handling - record dispatch failure
            _update_temporal_scheduler_status(
                last_workflow_id=workflow_id,
                last_task_id=status_id,
                last_result="error",
                last_started_at=_utc_timestamp(),
                last_completed_at=None,
                last_error=str(exc),
            )
            raise

        _update_temporal_scheduler_status(
            last_workflow_id=workflow_id,
            last_task_id=status_id,
            last_result="started",
            last_started_at=_utc_timestamp(),
            last_completed_at=None,
            last_error=None,
        )

        logger.info(
            "Temporal scheduled workflow started",
            work_id=status_id,
            workflow_id=workflow_id,
            task_queue=self.scheduler_config.temporal_task_queue,
        )

    async def _upsert_schedule(self, schedule_id: str, schedule: Schedule) -> None:
        if self.client is None:
            raise RuntimeError("Temporal scheduler runtime has not been started")
        try:
            await self.client.create_schedule(schedule_id, schedule)
        except ScheduleAlreadyRunningError:
            handle = self.client.get_schedule_handle(schedule_id)
            await handle.update(lambda _input: ScheduleUpdate(schedule=schedule))
        except RPCError as exc:
            if exc.status != RPCStatusCode.ALREADY_EXISTS:
                raise
            handle = self.client.get_schedule_handle(schedule_id)
            await handle.update(lambda _input: ScheduleUpdate(schedule=schedule))

    async def _delete_stale_schedules(self, desired_schedule_ids: set[str]) -> None:
        if self.client is None:
            raise RuntimeError("Temporal scheduler runtime has not been started")
        schedule_iter: Any = self.client.list_schedules()
        if inspect.isawaitable(schedule_iter):
            schedule_iter = await schedule_iter
        async for description in schedule_iter:
            schedule_id = description.id
            if not schedule_id.startswith(SCHEDULE_PREFIXES):
                continue
            if schedule_id in desired_schedule_ids:
                continue
            handle = self.client.get_schedule_handle(schedule_id)
            try:
                await handle.delete()
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
