"""Temporal schedule reconciliation helpers."""

from __future__ import annotations

import inspect
from collections.abc import (
    Awaitable,  # noqa: TC003, RUF100 - beartype resolves Temporal reconciler annotations at runtime.
    Callable,  # noqa: TC003, RUF100 - beartype resolves Temporal reconciler annotations at runtime.
)
from datetime import UTC, datetime
from pathlib import (
    Path,  # noqa: TC003, RUF100 - beartype resolves Temporal reconciler annotations at runtime.
)
from typing import Any, cast

from temporalio.client import (
    Schedule,
    ScheduleAlreadyRunningError,
    ScheduleUpdate,
)
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from pynchy.host.git_ops.repo import repo_host_root
from pynchy.host.orchestrator.temporal.schedules import (
    SCHEDULE_PREFIXES,
    agent_task_occurrence_due_at,
    agent_task_occurrence_workflow_id,
    agent_task_schedule_id,
    agent_task_workflow_id,
    canary_schedule_id,
    channel_reconciliation_schedule_id,
    config_host_cron_schedule_id,
    database_host_job_schedule_id,
    database_host_job_workflow_id,
    external_git_sync_schedule_id,
    host_git_sync_schedule_id,
    linear_work_item_reconciliation_schedule_id,
    once_due_at,
    schedule_for_agent_task,
    schedule_for_canaries,
    schedule_for_channel_reconciliation,
    schedule_for_config_host_cron,
    schedule_for_database_host_job,
    schedule_for_external_git_sync,
    schedule_for_host_git_sync,
    schedule_for_linear_work_item_reconciliation,
    start_delay_until,
)
from pynchy.host.orchestrator.temporal.workflows import (
    DatabaseHostJobWorkflow,
    ScheduledAgentTaskWorkflow,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves Temporal reconciler annotations at runtime.
    HostJob,
    ScheduledTask,
)

_TEMPORAL_SCHEDULER_RUNTIME_NOT_STARTED = "Temporal scheduler runtime has not been started"
_ONE_SHOT_WORKFLOW_PREFIX_BY_TYPE = {
    ScheduledAgentTaskWorkflow.__name__: "pynchy-agent-task-",
    DatabaseHostJobWorkflow.__name__: "pynchy-host-job-",
}


async def reconcile_temporal_schedules(
    runtime: object,
    *,
    get_settings_fn: Callable[[], object],
    get_tasks: Callable[[], Awaitable[list[ScheduledTask]]],
    get_host_jobs: Callable[[], Awaitable[list[HostJob]]],
) -> None:
    """Reconcile Pynchy's desired scheduled work into Temporal schedules."""
    client = cast("Any", _require_client(runtime))
    desired_schedule_ids: set[str] = set()
    settings = cast("Any", get_settings_fn())
    tasks = await get_tasks()
    host_jobs = await get_host_jobs()
    desired_once_workflow_ids = _desired_once_workflow_ids(tasks, host_jobs)
    deferred_once_task_ids = await _cancel_superseded_resumed_agent_workflows(client, tasks)

    await _reconcile_builtin_schedules(client, settings, desired_schedule_ids)
    await _reconcile_external_repo_sync_schedules(client, settings, desired_schedule_ids)
    await _reconcile_task_schedules(
        runtime,
        client,
        tasks,
        desired_schedule_ids,
        deferred_once_task_ids=deferred_once_task_ids,
    )
    await _reconcile_host_job_schedules(runtime, client, host_jobs, desired_schedule_ids)
    await _reconcile_config_cron_schedules(client, settings, desired_schedule_ids)

    await _cancel_stale_delayed_workflows(client, desired_once_workflow_ids)
    await _delete_stale_schedules(client, desired_schedule_ids)


def _desired_once_workflow_ids(tasks: list[ScheduledTask], host_jobs: list[HostJob]) -> set[str]:
    """Return delayed workflow IDs still owned by database rows.

    Inactive rows remain owners: their workflows enforce pause or disable semantics
    when they eventually run, and reconciliation may later reactivate the same row.
    """
    return {agent_task_workflow_id(task) for task in tasks if task.schedule_type == "once"} | {
        database_host_job_workflow_id(job) for job in host_jobs if job.schedule_type == "once"
    }


async def _reconcile_builtin_schedules(
    client: object, settings: object, desired_schedule_ids: set[str]
) -> None:
    client_any = cast("Any", client)
    host_sync_schedule_id = host_git_sync_schedule_id()
    desired_schedule_ids.add(host_sync_schedule_id)
    await _upsert_schedule(client_any, host_sync_schedule_id, schedule_for_host_git_sync())

    channel_schedule_id = channel_reconciliation_schedule_id()
    desired_schedule_ids.add(channel_schedule_id)
    await _upsert_schedule(
        client_any,
        channel_schedule_id,
        schedule_for_channel_reconciliation(),
    )
    work_item_schedule_id = linear_work_item_reconciliation_schedule_id()
    desired_schedule_ids.add(work_item_schedule_id)
    await _upsert_schedule(
        client_any,
        work_item_schedule_id,
        schedule_for_linear_work_item_reconciliation(),
    )
    settings_any = cast("Any", settings)
    if settings_any.canary.enabled:
        schedule_id = canary_schedule_id()
        desired_schedule_ids.add(schedule_id)
        await _upsert_schedule(client_any, schedule_id, schedule_for_canaries())


async def _reconcile_external_repo_sync_schedules(
    client: object,
    settings: object,
    desired_schedule_ids: set[str],
) -> None:
    client_any = cast("Any", client)
    for repo_slug in _external_repo_sync_slugs(settings):
        schedule_id = external_git_sync_schedule_id(repo_slug)
        desired_schedule_ids.add(schedule_id)
        await _upsert_schedule(client_any, schedule_id, schedule_for_external_git_sync(repo_slug))


async def _reconcile_task_schedules(
    runtime: object,
    client: object,
    tasks: list[ScheduledTask],
    desired_schedule_ids: set[str],
    *,
    deferred_once_task_ids: set[str],
) -> None:
    runtime_any = cast("Any", runtime)
    client_any = cast("Any", client)
    for task in tasks:
        if task.status != "active":
            continue
        if task.schedule_type == "once":
            if task.id in deferred_once_task_ids:
                continue
            await _start_once_agent_task(runtime_any, task)
            continue
        schedule_id = agent_task_schedule_id(task)
        desired_schedule_ids.add(schedule_id)
        await _upsert_schedule(client_any, schedule_id, schedule_for_agent_task(task))


async def _reconcile_host_job_schedules(
    runtime: object,
    client: object,
    host_jobs: list[HostJob],
    desired_schedule_ids: set[str],
) -> None:
    runtime_any = cast("Any", runtime)
    client_any = cast("Any", client)
    for job in host_jobs:
        if job.status != "active" or not job.enabled:
            continue
        if job.schedule_type == "once":
            await _start_once_database_host_job(runtime_any, job)
            continue
        schedule_id = database_host_job_schedule_id(job)
        desired_schedule_ids.add(schedule_id)
        await _upsert_schedule(client_any, schedule_id, schedule_for_database_host_job(job))


async def _reconcile_config_cron_schedules(
    client: object,
    settings: object,
    desired_schedule_ids: set[str],
) -> None:
    client_any = cast("Any", client)
    settings_any = cast("Any", settings)
    for job_name, job in settings_any.jobs.items():
        if not job.is_host or not job.enabled:
            continue
        if job.schedule is None:
            raise RuntimeError(f"validated host job {job_name!r} has no schedule")
        schedule_id = config_host_cron_schedule_id(job_name)
        desired_schedule_ids.add(schedule_id)
        await _upsert_schedule(
            client_any,
            schedule_id,
            schedule_for_config_host_cron(job_name, job.schedule),
        )


def _require_client(runtime: object) -> object:
    runtime_any = cast("Any", runtime)
    client = runtime_any.client
    if client is None:
        raise RuntimeError(_TEMPORAL_SCHEDULER_RUNTIME_NOT_STARTED)
    return client


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
        handle = client_any.get_schedule_handle(schedule_id)
        await handle.update(lambda _input: ScheduleUpdate(schedule=schedule))
    except RPCError as exc:
        if exc.status != RPCStatusCode.ALREADY_EXISTS:
            raise
        handle = client_any.get_schedule_handle(schedule_id)
        await handle.update(lambda _input: ScheduleUpdate(schedule=schedule))


async def _cancel_stale_delayed_workflows(client: object, desired_workflow_ids: set[str]) -> None:
    """Cancel future one-shot workflows that lack a database owner."""
    client_any = cast("Any", client)
    workflow_iter: object = client_any.list_workflows('ExecutionStatus = "Running"')
    if inspect.isawaitable(workflow_iter):
        workflow_iter = await cast("Any", workflow_iter)
    now = datetime.now(UTC)
    async for execution in cast("Any", workflow_iter):
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
    workflow_iter: object = client_any.list_workflows('ExecutionStatus = "Running"')
    if inspect.isawaitable(workflow_iter):
        workflow_iter = await cast("Any", workflow_iter)
    deferred: set[str] = set()
    async for execution in cast("Any", workflow_iter):
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


async def _delete_stale_schedules(client: object, desired_schedule_ids: set[str]) -> None:
    client_any = cast("Any", client)
    schedule_iter: object = client_any.list_schedules()
    if inspect.isawaitable(schedule_iter):
        schedule_iter = await cast("Any", schedule_iter)
    async for description in cast("Any", schedule_iter):
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


def _repo_root_for_slug(settings: object, repo_slug: str) -> Path | None:
    return repo_host_root(cast("Any", settings), repo_slug)


def _external_repo_sync_slugs(settings: object) -> list[str]:
    settings_any = cast("Any", settings)
    slugs: set[str] = set()
    for workspace_name in settings_any.workspaces:
        resolved = settings_any.resolved_workspace_config(workspace_name)
        if resolved is not None:
            slugs.update(resolved.repo)

    external: list[str] = []
    for repo_slug in sorted(slugs):
        repo_root = _repo_root_for_slug(settings, repo_slug)
        if repo_root is None:
            continue
        if repo_root.resolve() == settings_any.project_root.resolve():
            continue
        external.append(repo_slug)
    return external
