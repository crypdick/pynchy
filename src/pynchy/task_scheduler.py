"""Task scheduler — runs scheduled tasks on their due dates.

Port of src/task-scheduler.ts — async polling loop using asyncio.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from croniter import croniter

from pynchy.config import (
    DEFAULT_AGENT_CORE,
    GOD_GROUP_FOLDER,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    SCHEDULER_POLL_INTERVAL,
    TIMEZONE,
)
from pynchy.container_runner import run_container_agent, write_tasks_snapshot
from pynchy.db import (
    get_all_tasks,
    get_due_tasks,
    get_task_by_id,
    log_task_run,
    update_task_after_run,
)
from pynchy.group_queue import GroupQueue
from pynchy.logger import logger
from pynchy.types import ContainerInput, ContainerOutput, RegisteredGroup, ScheduledTask, TaskRunLog


class SchedulerDependencies(Protocol):
    """Dependencies for the task scheduler."""

    def registered_groups(self) -> dict[str, RegisteredGroup]: ...

    def get_sessions(self) -> dict[str, str]: ...

    @property
    def queue(self) -> GroupQueue: ...

    def on_process(
        self, group_jid: str, proc: Any, container_name: str, group_folder: str
    ) -> None: ...

    async def broadcast_to_channels(self, jid: str, text: str) -> None: ...

    @property
    def plugin_manager(self) -> Any: ...


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
    start_time = datetime.now(UTC)
    group_dir = GROUPS_DIR / task.group_folder
    group_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Running scheduled task", task_id=task.id, group=task.group_folder)

    groups = deps.registered_groups()
    group = next((g for g in groups.values() if g.folder == task.group_folder), None)

    if not group:
        logger.error(
            "Group not found for task",
            task_id=task.id,
            group_folder=task.group_folder,
        )
        await log_task_run(
            TaskRunLog(
                task_id=task.id,
                run_at=datetime.now(UTC).isoformat(),
                duration_ms=(datetime.now(UTC) - start_time).total_seconds() * 1000,
                status="error",
                result=None,
                error=f"Group not found: {task.group_folder}",
            )
        )
        return

    _is_god = task.group_folder == GOD_GROUP_FOLDER

    # Write tasks snapshot so the container can read current task state
    all_tasks = await get_all_tasks()
    write_tasks_snapshot(
        task.group_folder,
        _is_god,
        [
            {
                "id": t.id,
                "groupFolder": t.group_folder,
                "prompt": t.prompt,
                "schedule_type": t.schedule_type,
                "schedule_value": t.schedule_value,
                "status": t.status,
                "next_run": t.next_run,
            }
            for t in all_tasks
        ],
    )

    result: str | None = None
    error: str | None = None

    # For group context mode, use the group's current session
    sessions = deps.get_sessions()
    _session_id = sessions.get(task.group_folder) if task.context_mode == "group" else None

    # Idle timer: close container stdin after IDLE_TIMEOUT of no output,
    # so the container exits instead of hanging at waitForIpcMessage.
    idle_handle: asyncio.TimerHandle | None = None
    loop = asyncio.get_running_loop()

    def _reset_idle_timer() -> None:
        nonlocal idle_handle
        if idle_handle is not None:
            idle_handle.cancel()
        idle_handle = loop.call_later(
            IDLE_TIMEOUT,
            lambda: (
                logger.debug("Scheduled task idle timeout, closing stdin", task_id=task.id),
                deps.queue.close_stdin(task.chat_jid),
            ),
        )

    try:
        # Convert task prompt to SDK message format
        task_messages = [
            {
                "message_type": "user",
                "sender": "scheduled_task",
                "sender_name": "Scheduled Task",
                "content": task.prompt,
                "timestamp": datetime.now(UTC).isoformat(),
                "metadata": {"source": "scheduled_task"},
            }
        ]

        # Look up agent core plugin by configured name
        agent_core_module = "agent_runner.cores.claude"
        agent_core_class = "ClaudeAgentCore"
        if deps.plugin_manager:
            cores = deps.plugin_manager.hook.pynchy_agent_core_info()
            desired = DEFAULT_AGENT_CORE
            core_info = next((c for c in cores if c["name"] == desired), None)
            if core_info is None and cores:
                core_info = cores[0]
            if core_info:
                agent_core_module = core_info["module"]
                agent_core_class = core_info["class_name"]

        container_input = ContainerInput(
            messages=task_messages,
            group_folder=task.group_folder,
            chat_jid=task.chat_jid,
            is_god=_is_god,
            session_id=_session_id,
            is_scheduled_task=True,
            project_access=task.project_access,
            agent_core_module=agent_core_module,
            agent_core_class=agent_core_class,
        )

        async def _on_streamed_output(streamed: ContainerOutput) -> None:
            nonlocal result, error
            if streamed.result:
                result = streamed.result
                await deps.broadcast_to_channels(task.chat_jid, streamed.result)
                _reset_idle_timer()
            if streamed.status == "error":
                error = streamed.error or "Unknown error"

        output = await run_container_agent(
            group=group,
            input_data=container_input,
            on_process=lambda proc, name: deps.on_process(
                task.chat_jid, proc, name, task.group_folder
            ),
            on_output=_on_streamed_output,
            plugin_manager=deps.plugin_manager,
        )

        if idle_handle is not None:
            idle_handle.cancel()

        if output.status == "error":
            error = output.error or "Unknown error"
        elif output.result:
            result = output.result

        elapsed_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
        logger.info("Task completed", task_id=task.id, duration_ms=elapsed_ms)

        # Merge worktree commits and push for all project_access tasks
        if not error and task.project_access:
            from pynchy.http_server import _push_local_commits
            from pynchy.worktree import merge_worktree

            if merge_worktree(task.group_folder):
                _push_local_commits()
    except Exception as exc:
        if idle_handle is not None:
            idle_handle.cancel()
        error = str(exc)
        logger.error("Task failed", task_id=task.id, error=error)

    duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000

    await log_task_run(
        TaskRunLog(
            task_id=task.id,
            run_at=datetime.now(UTC).isoformat(),
            duration_ms=duration_ms,
            status="error" if error else "success",
            result=result,
            error=error,
        )
    )

    # Calculate next run
    next_run: str | None = None
    if task.schedule_type == "cron":
        tz = ZoneInfo(TIMEZONE)
        cron = croniter(task.schedule_value, datetime.now(tz))
        next_run = cron.get_next(datetime).isoformat()
    elif task.schedule_type == "interval":
        ms = int(task.schedule_value)
        next_run = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() + ms / 1000,
            tz=UTC,
        ).isoformat()
    # 'once' tasks have no next run

    result_summary = f"Error: {error}" if error else (result[:200] if result else "Completed")
    await update_task_after_run(task.id, next_run, result_summary)
