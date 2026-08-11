"""Application evidence joined to Temporal schedule state for /status."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pynchy.scheduling.api import (  # type aliases evaluate at module import.
    HostJob,
    ScheduledTask,
    TaskRunLog,
    scheduled_work_attention,
)

TemporalStateReader = Callable[
    [list[ScheduledTask], list[HostJob], str, str],
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
    temporal_connection: tuple[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return schedule definitions enriched with Temporal-owned execution state."""
    tasks, jobs = await asyncio.gather(get_tasks(), get_host_jobs())
    orchestration = await get_temporal_states(tasks, jobs, *temporal_connection)
    task_logs = await asyncio.gather(
        *(get_task_logs(task.id) for task in tasks),
    )
    task_status = []
    for task, logs in zip(tasks, task_logs, strict=True):
        run_health = _task_run_health(logs)
        temporal_state = orchestration["task", task.id]
        task_status.append(
            {
                "id": task.id,
                "group": task.group_folder,
                "schedule_type": task.schedule_type,
                "schedule_value": task.schedule_value,
                "status": task.status,
                "next_run": temporal_state["next_run"],
                "orchestration": temporal_state,
                "last_run": task.last_run,
                "last_result": task.last_result,
                "run_health": run_health,
                "attention": list(
                    scheduled_work_attention(
                        status=task.status,
                        next_run=temporal_state["next_run"],
                        consecutive_failures=run_health["consecutive_failures"],
                        orchestration_error=temporal_state["error"],
                        last_result=task.last_result,
                    )
                ),
            }
        )
    host_job_status = []
    for job in jobs:
        temporal_state = orchestration["host_job", job.id]
        host_job_status.append(
            {
                "id": job.id,
                "name": job.name,
                "schedule_type": job.schedule_type,
                "schedule_value": job.schedule_value,
                "status": job.status,
                "enabled": job.enabled,
                "next_run": temporal_state["next_run"],
                "orchestration": temporal_state,
                "last_run": job.last_run,
                "attention": list(
                    scheduled_work_attention(
                        status=job.status,
                        next_run=temporal_state["next_run"],
                        orchestration_error=temporal_state["error"],
                    )
                ),
            }
        )
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
        "last_temporal_workflow_run_id": last.temporal_workflow_run_id if last else None,
        "last_temporal_attempt": last.temporal_attempt if last else None,
        "last_turn_id": last.turn_id if last else None,
        "escalation_reason": last.escalation_reason if last else None,
    }
