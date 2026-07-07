"""Temporal runtime for scheduled Pynchy work.

Temporal owns durable execution; activities delegate to the existing host
runner so container IPC and streaming behavior stay in one place.
"""

from __future__ import annotations

import contextlib
import inspect
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

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
from pynchy.host.orchestrator.task_scheduler import (
    SchedulerDependencies,
    _run_scheduled_agent,
    resolve_cron_job_cwd,
)
from pynchy.host.orchestrator.temporal.schedules import (
    SCHEDULE_PREFIXES,
    agent_task_schedule_id,
    agent_task_workflow_id,
    config_host_cron_schedule_id,
    database_host_job_schedule_id,
    database_host_job_workflow_id,
    once_due_at,
    schedule_for_agent_task,
    schedule_for_config_host_cron,
    schedule_for_database_host_job,
    start_delay_until,
)
from pynchy.host.orchestrator.temporal.workflows import (
    ConfigHostCronWorkflow,
    DatabaseHostJobWorkflow,
    ScheduledAgentTaskWorkflow,
)
from pynchy.logger import logger
from pynchy.state import (
    get_all_host_jobs,
    get_all_tasks,
    get_host_job_by_id,
    get_task_by_id,
    update_host_job_after_run,
)
from pynchy.types import HostJob, ScheduledTask
from pynchy.utils import compute_next_run, log_shell_result, run_shell_command

_scheduler_deps: SchedulerDependencies | None = None
_WORKFLOW_MODULE = "pynchy.host.orchestrator.temporal.workflows"


@dataclass(frozen=True)
class TemporalSchedulerStatusSnapshot:
    worker_running: bool = False
    last_workflow_id: str | None = None
    last_task_id: str | None = None
    last_result: str | None = None
    last_started_at: str | None = None
    last_completed_at: str | None = None
    last_error: str | None = None


_temporal_scheduler_status = TemporalSchedulerStatusSnapshot()


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def reset_temporal_scheduler_status() -> None:
    """Clear the in-process Temporal worker status snapshot."""
    global _temporal_scheduler_status
    _temporal_scheduler_status = TemporalSchedulerStatusSnapshot()


def get_temporal_scheduler_status() -> dict[str, Any]:
    """Return the in-process Temporal worker status snapshot."""
    return asdict(_temporal_scheduler_status)


def _update_temporal_scheduler_status(**changes: Any) -> None:
    global _temporal_scheduler_status
    _temporal_scheduler_status = replace(_temporal_scheduler_status, **changes)


def _activity_workflow_id() -> str | None:
    try:
        return activity.info().workflow_id
    except RuntimeError:
        return None


def _record_activity_result(task_id: str, result: str, error: str | None = None) -> None:
    _update_temporal_scheduler_status(
        last_workflow_id=_activity_workflow_id(),
        last_task_id=task_id,
        last_result=result,
        last_completed_at=_utc_timestamp(),
        last_error=error,
    )


def bind_scheduler_deps(deps: SchedulerDependencies | None) -> None:
    """Bind app dependencies for Temporal activities running in this process."""
    global _scheduler_deps
    _scheduler_deps = deps


def _require_scheduler_deps() -> SchedulerDependencies:
    if _scheduler_deps is None:
        raise RuntimeError("Temporal scheduler dependencies are not bound")
    return _scheduler_deps


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


@activity.defn(name="run_database_host_job")
async def run_database_host_job(job_id: str) -> str:
    """Temporal activity that runs one active database-backed host job."""
    job = await get_host_job_by_id(job_id)
    if job is None or job.status != "active" or not job.enabled:
        logger.info("Temporal database host job skipped", job_id=job_id)
        _record_activity_result(job_id, "skipped")
        return "skipped"

    try:
        await _run_database_host_job(job)
    except Exception as exc:  # allow: exception-handling - record activity failure
        _record_activity_result(job_id, "error", str(exc))
        raise
    _record_activity_result(job_id, "completed")
    return "completed"


@activity.defn(name="run_config_host_cron_job")
async def run_config_host_cron_job(job_name: str) -> str:
    """Temporal activity that runs one enabled config-backed host cron job."""
    job = get_settings().cron_jobs.get(job_name)
    if job is None or not job.enabled:
        logger.info("Temporal config host cron job skipped", job=job_name)
        _record_activity_result(job_name, "skipped")
        return "skipped"

    try:
        command_cwd = resolve_cron_job_cwd(job.cwd)
        logger.info(
            "Running config host cron job",
            job=job_name,
            schedule=job.schedule,
            cwd=command_cwd,
        )
        result = await run_shell_command(
            job.command,
            cwd=command_cwd,
            timeout_seconds=job.timeout_seconds,
        )
        log_shell_result(result, label="Config host cron job", job=job_name)
    except Exception as exc:  # allow: exception-handling - record activity failure
        _record_activity_result(job_name, "error", str(exc))
        raise
    _record_activity_result(job_name, "completed")
    return "completed"


async def _run_database_host_job(job: HostJob) -> None:
    command_cwd = resolve_cron_job_cwd(job.cwd)
    logger.info(
        "Running database host job",
        job_id=job.id,
        name=job.name,
        schedule_type=job.schedule_type,
        cwd=command_cwd,
    )

    result = await run_shell_command(
        job.command,
        cwd=command_cwd,
        timeout_seconds=job.timeout_seconds,
    )
    log_shell_result(result, label="Database host job", job_id=job.id)

    next_run = compute_next_run(job.schedule_type, job.schedule_value, get_settings().timezone)
    exit_code = result.returncode if result.returncode is not None else 1
    await update_host_job_after_run(job.id, next_run, exit_code)


class TemporalSchedulerRuntime:
    """Owns the Temporal client, worker, and schedule reconciliation."""

    def __init__(self, deps: SchedulerDependencies, scheduler_config: SchedulerConfig) -> None:
        self.deps = deps
        self.scheduler_config = scheduler_config
        self.client: Client | None = None
        self._worker: Worker | None = None
        self._worker_stack = contextlib.AsyncExitStack()

    async def __aenter__(self) -> TemporalSchedulerRuntime:
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
                ],
                activities=[
                    run_scheduled_agent_task,
                    run_database_host_job,
                    run_config_host_cron_job,
                ],
                workflow_runner=scheduler_workflow_runner(),
            )
            await self._worker_stack.enter_async_context(self._worker)
        except BaseException as exc:  # allow: exception-handling - startup cleanup then re-raise
            await self._worker_stack.aclose()
            bind_scheduler_deps(None)
            _update_temporal_scheduler_status(worker_running=False, last_error=str(exc))
            raise
        _update_temporal_scheduler_status(worker_running=True, last_error=None)
        logger.info(
            "Temporal scheduler runtime started",
            address=self.scheduler_config.temporal_address,
            namespace=self.scheduler_config.temporal_namespace,
            task_queue=self.scheduler_config.temporal_task_queue,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._worker_stack.aclose()
        bind_scheduler_deps(None)
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
        arg: str,
        *,
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
                    arg,
                    id=workflow_id,
                    task_queue=self.scheduler_config.temporal_task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            else:
                await self.client.start_workflow(
                    workflow,
                    arg,
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
