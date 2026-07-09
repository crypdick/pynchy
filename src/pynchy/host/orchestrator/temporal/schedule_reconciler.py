"""Temporal schedule reconciliation helpers."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from temporalio.client import (
    Schedule,
    ScheduleAlreadyRunningError,
    ScheduleUpdate,
)
from temporalio.service import RPCError, RPCStatusCode

from pynchy.host.orchestrator.temporal.schedules import (
    SCHEDULE_PREFIXES,
    agent_task_schedule_id,
    agent_task_workflow_id,
    channel_reconciliation_schedule_id,
    config_host_cron_schedule_id,
    database_host_job_schedule_id,
    database_host_job_workflow_id,
    external_git_sync_schedule_id,
    host_git_sync_schedule_id,
    once_due_at,
    schedule_for_agent_task,
    schedule_for_channel_reconciliation,
    schedule_for_config_host_cron,
    schedule_for_database_host_job,
    schedule_for_external_git_sync,
    schedule_for_host_git_sync,
    start_delay_until,
)
from pynchy.host.orchestrator.temporal.workflows import (
    DatabaseHostJobWorkflow,
    ScheduledAgentTaskWorkflow,
)
from pynchy.types import HostJob, ScheduledTask


async def reconcile_temporal_schedules(
    runtime: Any,
    *,
    get_settings_fn: Callable[[], Any],
    get_tasks: Callable[[], Awaitable[list[ScheduledTask]]],
    get_host_jobs: Callable[[], Awaitable[list[HostJob]]],
) -> None:
    """Reconcile Pynchy's desired scheduled work into Temporal schedules."""
    client = _require_client(runtime)
    desired_schedule_ids: set[str] = set()
    settings = get_settings_fn()
    tasks = await get_tasks()
    host_jobs = await get_host_jobs()

    await _reconcile_builtin_schedules(client, desired_schedule_ids)
    await _reconcile_external_repo_sync_schedules(client, settings, desired_schedule_ids)
    await _reconcile_task_schedules(runtime, client, tasks, desired_schedule_ids)
    await _reconcile_host_job_schedules(runtime, client, host_jobs, desired_schedule_ids)
    await _reconcile_config_cron_schedules(client, settings, desired_schedule_ids)

    await _delete_stale_schedules(client, desired_schedule_ids)


async def _reconcile_builtin_schedules(client: Any, desired_schedule_ids: set[str]) -> None:
    host_sync_schedule_id = host_git_sync_schedule_id()
    desired_schedule_ids.add(host_sync_schedule_id)
    await _upsert_schedule(client, host_sync_schedule_id, schedule_for_host_git_sync())

    channel_schedule_id = channel_reconciliation_schedule_id()
    desired_schedule_ids.add(channel_schedule_id)
    await _upsert_schedule(
        client,
        channel_schedule_id,
        schedule_for_channel_reconciliation(),
    )


async def _reconcile_external_repo_sync_schedules(
    client: Any,
    settings: Any,
    desired_schedule_ids: set[str],
) -> None:
    for repo_slug in _external_repo_sync_slugs(settings):
        schedule_id = external_git_sync_schedule_id(repo_slug)
        desired_schedule_ids.add(schedule_id)
        await _upsert_schedule(client, schedule_id, schedule_for_external_git_sync(repo_slug))


async def _reconcile_task_schedules(
    runtime: Any,
    client: Any,
    tasks: list[ScheduledTask],
    desired_schedule_ids: set[str],
) -> None:
    for task in tasks:
        if task.status != "active":
            continue
        if task.schedule_type == "once":
            await _start_once_agent_task(runtime, task)
            continue
        schedule_id = agent_task_schedule_id(task)
        desired_schedule_ids.add(schedule_id)
        await _upsert_schedule(client, schedule_id, schedule_for_agent_task(task))


async def _reconcile_host_job_schedules(
    runtime: Any,
    client: Any,
    host_jobs: list[HostJob],
    desired_schedule_ids: set[str],
) -> None:
    for job in host_jobs:
        if job.status != "active" or not job.enabled:
            continue
        if job.schedule_type == "once":
            await _start_once_database_host_job(runtime, job)
            continue
        schedule_id = database_host_job_schedule_id(job)
        desired_schedule_ids.add(schedule_id)
        await _upsert_schedule(client, schedule_id, schedule_for_database_host_job(job))


async def _reconcile_config_cron_schedules(
    client: Any,
    settings: Any,
    desired_schedule_ids: set[str],
) -> None:
    for job_name, cron_job in settings.cron_jobs.items():
        if not cron_job.enabled:
            continue
        schedule_id = config_host_cron_schedule_id(job_name)
        desired_schedule_ids.add(schedule_id)
        await _upsert_schedule(
            client,
            schedule_id,
            schedule_for_config_host_cron(job_name, cron_job.schedule),
        )


def _require_client(runtime: Any) -> Any:
    client = runtime.client
    if client is None:
        raise RuntimeError("Temporal scheduler runtime has not been started")
    return client


async def _start_once_agent_task(runtime: Any, task: ScheduledTask) -> None:
    workflow_id = agent_task_workflow_id(task)
    await runtime._start_workflow(
        ScheduledAgentTaskWorkflow.run,
        task.id,
        workflow_id=workflow_id,
        status_id=task.id,
        start_delay=start_delay_until(once_due_at(task.next_run or task.schedule_value)),
    )


async def _start_once_database_host_job(runtime: Any, job: HostJob) -> None:
    workflow_id = database_host_job_workflow_id(job)
    await runtime._start_workflow(
        DatabaseHostJobWorkflow.run,
        job.id,
        workflow_id=workflow_id,
        status_id=job.id,
        start_delay=start_delay_until(once_due_at(job.next_run or job.schedule_value)),
    )


async def _upsert_schedule(client: Any, schedule_id: str, schedule: Schedule) -> None:
    try:
        await client.create_schedule(schedule_id, schedule)
    except ScheduleAlreadyRunningError:
        handle = client.get_schedule_handle(schedule_id)
        await handle.update(lambda _input: ScheduleUpdate(schedule=schedule))
    except RPCError as exc:
        if exc.status != RPCStatusCode.ALREADY_EXISTS:
            raise
        handle = client.get_schedule_handle(schedule_id)
        await handle.update(lambda _input: ScheduleUpdate(schedule=schedule))


async def _delete_stale_schedules(client: Any, desired_schedule_ids: set[str]) -> None:
    schedule_iter: Any = client.list_schedules()
    if inspect.isawaitable(schedule_iter):
        schedule_iter = await schedule_iter
    async for description in schedule_iter:
        schedule_id = description.id
        if not schedule_id.startswith(SCHEDULE_PREFIXES):
            continue
        if schedule_id in desired_schedule_ids:
            continue
        handle = client.get_schedule_handle(schedule_id)
        try:
            await handle.delete()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise


def _repo_root_for_slug(settings: Any, repo_slug: str) -> Path | None:
    repo_cfg = settings.repos.overrides.get(repo_slug)
    if repo_cfg is None:
        return None
    if repo_cfg.path:
        return Path(repo_cfg.path)
    try:
        owner, repo_name = repo_slug.split("/", 1)
    except ValueError:
        return None
    return Path(settings.repos.root) / owner / repo_name


def _external_repo_sync_slugs(settings: Any) -> list[str]:
    slugs: set[str] = set()
    for workspace_name in settings.workspaces:
        resolved = settings.resolved_workspace_config(workspace_name)
        if resolved is not None:
            slugs.update(resolved.repo)

    external: list[str] = []
    for repo_slug in sorted(slugs):
        repo_root = _repo_root_for_slug(settings, repo_slug)
        if repo_root is None:
            continue
        if repo_root.resolve() == settings.project_root.resolve():
            continue
        external.append(repo_slug)
    return external
