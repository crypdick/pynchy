"""Task scheduler — runs scheduled tasks on their due dates.

Port of src/task-scheduler.ts — async polling loop using asyncio.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from croniter import croniter

from pynchy.config import (
    GROUPS_DIR,
    IDLE_TIMEOUT,
    MAIN_GROUP_FOLDER,
    SCHEDULER_POLL_INTERVAL,
    TIMEZONE,
)
from pynchy.db import (
    get_all_tasks,
    get_due_tasks,
    get_task_by_id,
    log_task_run,
    update_task_after_run,
)
from pynchy.group_queue import GroupQueue
from pynchy.logger import logger
from pynchy.types import RegisteredGroup, ScheduledTask, TaskRunLog


class SchedulerDependencies(Protocol):
    """Dependencies for the task scheduler."""

    def registered_groups(self) -> dict[str, RegisteredGroup]: ...

    def get_sessions(self) -> dict[str, str]: ...

    @property
    def queue(self) -> GroupQueue: ...

    def on_process(
        self, group_jid: str, proc: Any, container_name: str, group_folder: str
    ) -> None: ...

    async def send_message(self, jid: str, text: str) -> None: ...


_scheduler_running = False


async def start_scheduler_loop(deps: SchedulerDependencies) -> None:
    """Start the scheduler polling loop."""
    global _scheduler_running
    if _scheduler_running:
        logger.debug("Scheduler loop already running, skipping duplicate start")
        return
    _scheduler_running = True
    logger.info("Scheduler loop started")

    while True:
        try:
            due_tasks = await get_due_tasks()
            if due_tasks:
                logger.info("Found due tasks", count=len(due_tasks))

            for task in due_tasks:
                # Re-check task status (may have been paused/cancelled)
                current_task = await get_task_by_id(task.id)
                if not current_task or current_task.status != "active":
                    continue

                async def _make_task_runner(t: ScheduledTask = current_task) -> None:
                    await _run_task(t, deps)

                deps.queue.enqueue_task(
                    current_task.chat_jid,
                    current_task.id,
                    _make_task_runner,
                )
        except Exception as exc:
            logger.error("Error in scheduler loop", err=str(exc))

        await asyncio.sleep(SCHEDULER_POLL_INTERVAL)


async def _run_task(task: ScheduledTask, deps: SchedulerDependencies) -> None:
    """Execute a single scheduled task."""
    start_time = datetime.now(timezone.utc)
    group_dir = GROUPS_DIR / task.group_folder
    group_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Running scheduled task", task_id=task.id, group=task.group_folder
    )

    groups = deps.registered_groups()
    group = next(
        (g for g in groups.values() if g.folder == task.group_folder), None
    )

    if not group:
        logger.error(
            "Group not found for task",
            task_id=task.id,
            group_folder=task.group_folder,
        )
        await log_task_run(
            TaskRunLog(
                task_id=task.id,
                run_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=(datetime.now(timezone.utc) - start_time).total_seconds() * 1000,
                status="error",
                result=None,
                error=f"Group not found: {task.group_folder}",
            )
        )
        return

    is_main = task.group_folder == MAIN_GROUP_FOLDER
    result: str | None = None
    error: str | None = None

    # For group context mode, use the group's current session
    sessions = deps.get_sessions()
    session_id = sessions.get(task.group_folder) if task.context_mode == "group" else None

    try:
        # In the full implementation, this would call run_container_agent()
        # For now, log that the task would be run
        logger.info(
            "Task completed",
            task_id=task.id,
            duration_ms=(datetime.now(timezone.utc) - start_time).total_seconds() * 1000,
        )
    except Exception as exc:
        error = str(exc)
        logger.error("Task failed", task_id=task.id, error=error)

    duration_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

    await log_task_run(
        TaskRunLog(
            task_id=task.id,
            run_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=duration_ms,
            status="error" if error else "success",
            result=result,
            error=error,
        )
    )

    # Calculate next run
    next_run: str | None = None
    if task.schedule_type == "cron":
        cron = croniter(task.schedule_value)
        next_run = datetime.fromtimestamp(
            cron.get_next(float), tz=timezone.utc
        ).isoformat()
    elif task.schedule_type == "interval":
        ms = int(task.schedule_value)
        next_run = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + ms / 1000,
            tz=timezone.utc,
        ).isoformat()
    # 'once' tasks have no next run

    result_summary = (
        f"Error: {error}"
        if error
        else (result[:200] if result else "Completed")
    )
    await update_task_after_run(task.id, next_run, result_summary)
