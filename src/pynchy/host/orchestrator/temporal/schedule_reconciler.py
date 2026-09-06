"""Temporal schedule reconciliation helpers."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
    Collection,
)
from datetime import UTC, datetime
from typing import Any, cast

from temporalio.client import (
    Schedule,
    ScheduleAlreadyRunningError,
    ScheduleUpdate,
)
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from pynchy.host.orchestrator.scheduler_deps import (
    SchedulerRuntimeConfig,
)
from pynchy.host.orchestrator.temporal.schedules import (
    SCHEDULE_PREFIXES,
    agent_task_workflow_id,
    database_host_job_workflow_id,
    desired_recurring_schedules,
    once_due_at,
    start_delay_until,
)
from pynchy.host.orchestrator.temporal.workflows import (
    DatabaseHostJobWorkflow,
    ScheduledAgentTaskWorkflow,
)
from pynchy.scheduling.api import (  # beartype resolves Temporal reconciler annotations at runtime.
    HostJob,
    ScheduledTask,
    agent_task_occurrence_due_at,
    agent_task_occurrence_workflow_id,
)

_ONE_SHOT_WORKFLOW_PREFIX_BY_TYPE = {
    ScheduledAgentTaskWorkflow.__name__: "pynchy-agent-task-",
    DatabaseHostJobWorkflow.__name__: "pynchy-host-job-",
}


async def reconcile_temporal_schedules(
    runtime: object,
    *,
    scheduler_runtime: SchedulerRuntimeConfig,
    get_tasks: Callable[[], Awaitable[list[ScheduledTask]]],
    get_host_jobs: Callable[[], Awaitable[list[HostJob]]],
) -> None:
    """Reconcile Pynchy's desired scheduled work into Temporal schedules."""
    runtime_any = cast("Any", runtime)
    client = cast("Any", runtime_any.client)
    tasks = await get_tasks()
    host_jobs = await get_host_jobs()
    schedules = desired_recurring_schedules(tasks, host_jobs, scheduler_runtime)
    desired_once_workflow_ids = _desired_once_workflow_ids(tasks, host_jobs)
    deferred_once_task_ids = await _cancel_superseded_resumed_agent_workflows(client, tasks)

    for schedule_id, schedule in schedules.items():
        await _upsert_schedule(client, schedule_id, schedule)
    for task in tasks:
        if (
            task.status == "active"
            and task.schedule_type == "once"
            and task.id not in deferred_once_task_ids
        ):
            await _start_once_agent_task(runtime, task)
    for job in host_jobs:
        if job.status == "active" and job.enabled and job.schedule_type == "once":
            await _start_once_database_host_job(runtime, job)

    await _cancel_stale_delayed_workflows(client, desired_once_workflow_ids)
    await _delete_stale_schedules(client, schedules.keys())


def _desired_once_workflow_ids(tasks: list[ScheduledTask], host_jobs: list[HostJob]) -> set[str]:
    """Return delayed workflow IDs still owned by database rows.

    Inactive rows remain owners: their workflows enforce pause or disable semantics
    when they eventually run, and reconciliation may later reactivate the same row.
    """
    return {agent_task_workflow_id(task) for task in tasks if task.schedule_type == "once"} | {
        database_host_job_workflow_id(job) for job in host_jobs if job.schedule_type == "once"
    }


async def _start_once_agent_task(runtime: object, task: ScheduledTask) -> None:
    runtime_any = cast("Any", runtime)
    workflow_id = agent_task_workflow_id(task)
    await runtime_any.start_temporal_workflow(
        ScheduledAgentTaskWorkflow.run,
        task.id,
        workflow_id=workflow_id,
        status_id=task.id,
        start_delay=start_delay_until(once_due_at(agent_task_occurrence_due_at(task))),
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
    )


async def _start_once_database_host_job(runtime: object, job: HostJob) -> None:
    runtime_any = cast("Any", runtime)
    workflow_id = database_host_job_workflow_id(job)
    await runtime_any.start_temporal_workflow(
        DatabaseHostJobWorkflow.run,
        job.id,
        workflow_id=workflow_id,
        status_id=job.id,
        start_delay=start_delay_until(once_due_at(job.schedule_value)),
    )


async def _upsert_schedule(client: object, schedule_id: str, schedule: Schedule) -> None:
    client_any = cast("Any", client)
    try:
        await client_any.create_schedule(schedule_id, schedule)
    except ScheduleAlreadyRunningError:
        pass
    except RPCError as exc:
        if exc.status != RPCStatusCode.ALREADY_EXISTS:
            raise
    else:
        return
    handle = client_any.get_schedule_handle(schedule_id)
    await handle.update(lambda _input: ScheduleUpdate(schedule=schedule))


async def _cancel_stale_delayed_workflows(client: object, desired_workflow_ids: set[str]) -> None:
    """Cancel future one-shot workflows that lack a database owner."""
    client_any = cast("Any", client)
    workflow_iter = client_any.list_workflows('ExecutionStatus = "Running"')
    now = datetime.now(UTC)
    async for execution in workflow_iter:
        workflow_id = execution.id
        expected_prefix = _ONE_SHOT_WORKFLOW_PREFIX_BY_TYPE.get(execution.workflow_type)
        if expected_prefix is None or not workflow_id.startswith(expected_prefix):
            continue
        if workflow_id in desired_workflow_ids:
            continue
        execution_time = execution.execution_time
        if execution_time is None or execution_time <= now:
            continue
        handle = client_any.get_workflow_handle(workflow_id, run_id=execution.run_id)
        try:
            await handle.cancel()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise


async def _cancel_superseded_resumed_agent_workflows(
    client: object,
    tasks: list[ScheduledTask],
) -> set[str]:
    """Cancel an older running occurrence before starting an explicit resume."""
    once_tasks = [task for task in tasks if task.schedule_type == "once"]
    superseded_owners: dict[str, list[ScheduledTask]] = {}
    for task in once_tasks:
        if (
            task.status != "active"
            or task.superseded_occurrence_due_at is None
            or task.superseded_occurrence_generation is None
        ):
            continue
        workflow_id = agent_task_occurrence_workflow_id(
            task.id,
            task.superseded_occurrence_due_at,
            task.superseded_occurrence_generation,
        )
        superseded_owners.setdefault(workflow_id, []).append(task)
    if not superseded_owners:
        return set()

    current_owners: dict[str, set[str]] = {}
    for task in once_tasks:
        current_owners.setdefault(agent_task_workflow_id(task), set()).add(task.id)

    client_any = cast("Any", client)
    workflow_iter = client_any.list_workflows('ExecutionStatus = "Running"')
    deferred: set[str] = set()
    async for execution in workflow_iter:
        if execution.workflow_type != ScheduledAgentTaskWorkflow.__name__:
            continue
        owners = superseded_owners.get(execution.id, [])
        if not owners:
            continue

        # A generation-zero ID may normalize two distinct task IDs to the same
        # value. Never cancel or block on ambiguous ownership: resumed
        # generations use digested IDs and can safely make progress independently.
        if len(owners) != 1:
            continue
        owner = owners[0]
        if current_owners.get(execution.id, set()) - {owner.id}:
            continue

        deferred.add(owner.id)
        handle = client_any.get_workflow_handle(execution.id, run_id=execution.run_id)
        try:
            await handle.cancel()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
    return deferred


async def _delete_stale_schedules(client: object, desired_schedule_ids: Collection[str]) -> None:
    client_any = cast("Any", client)
    async for description in await client_any.list_schedules():
        schedule_id = description.id
        if not schedule_id.startswith(SCHEDULE_PREFIXES):
            continue
        if schedule_id in desired_schedule_ids:
            continue
        handle = client_any.get_schedule_handle(schedule_id)
        try:
            await handle.delete()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
