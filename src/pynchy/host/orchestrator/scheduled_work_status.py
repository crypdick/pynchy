"""Application evidence joined to Temporal schedule state for /status."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pynchy.types import (  # noqa: TC001, RUF100 - type aliases evaluate at module import.
    HostJob,
    ScheduledTask,
    TaskRunLog,
)

TemporalStateReader = Callable[
    [list[ScheduledTask], list[HostJob]],
    Awaitable[dict[tuple[str, str], dict[str, Any]]],
]
TaskReader = Callable[[], Awaitable[list[ScheduledTask]]]
HostJobReader = Callable[[], Awaitable[list[HostJob]]]
TaskLogReader = Callable[[str], Awaitable[list[TaskRunLog]]]


async def collect_scheduled_work(
    get_tasks: TaskReader,
    get_host_jobs: HostJobReader,
    get_task_logs: TaskLogReader,
    get_temporal_states: TemporalStateReader,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return schedule definitions enriched with Temporal-owned execution state."""
    tasks, jobs = await asyncio.gather(get_tasks(), get_host_jobs())
    orchestration = await get_temporal_states(tasks, jobs)
    task_logs = await asyncio.gather(
        *(get_task_logs(task.id) for task in tasks),
    )
    task_status = [
        {
            "id": task.id,
            "group": task.group_folder,
            "schedule_type": task.schedule_type,
            "schedule_value": task.schedule_value,
            "status": task.status,
            "next_run": orchestration["task", task.id]["next_run"],
            "orchestration": orchestration["task", task.id],
            "last_run": task.last_run,
            "last_result": task.last_result,
            "run_health": _task_run_health(logs),
        }
        for task, logs in zip(tasks, task_logs, strict=True)
    ]
    host_job_status = [
        {
            "id": job.id,
            "name": job.name,
            "schedule_type": job.schedule_type,
            "schedule_value": job.schedule_value,
            "status": job.status,
            "enabled": job.enabled,
            "next_run": orchestration["host_job", job.id]["next_run"],
            "orchestration": orchestration["host_job", job.id],
            "last_run": job.last_run,
        }
        for job in jobs
    ]
    return task_status, host_job_status


def _task_run_health(logs: list[TaskRunLog]) -> dict[str, Any]:
    """Summarize recent scheduled-task attempts for operator status."""
    last = logs[0] if logs else None
    consecutive_failures = 0
    for log in logs:
        if log.status != "error":
            break
        consecutive_failures += 1

    return {
        "last_status": last.status if last else None,
        "consecutive_failures": consecutive_failures,
        "last_error_signature": last.error_signature if last else None,
        "last_temporal_workflow_id": last.temporal_workflow_id if last else None,
        "last_temporal_attempt": last.temporal_attempt if last else None,
        "escalation_reason": last.escalation_reason if last else None,
    }
