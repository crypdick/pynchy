"""Task scheduler — runs scheduled tasks on their due dates."""

from __future__ import annotations

import asyncio
import contextlib
from asyncio.subprocess import PIPE
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from croniter import croniter

from pynchy.config import get_settings
from pynchy.container_runner import (
    resolve_agent_core,
    run_container_agent,
    write_tasks_snapshot,
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
from pynchy.router import format_tool_preview
from pynchy.types import ContainerInput, ContainerOutput, RegisteredGroup, ScheduledTask, TaskRunLog
from pynchy.utils import compute_next_run

if TYPE_CHECKING:
    import pluggy


class SchedulerDependencies(Protocol):
    """Dependencies for the task scheduler."""

    def registered_groups(self) -> dict[str, RegisteredGroup]: ...

    def get_sessions(self) -> dict[str, str]: ...

    @property
    def queue(self) -> GroupQueue: ...

    def on_process(
        self,
        group_jid: str,
        proc: asyncio.subprocess.Process | None,
        container_name: str,
        group_folder: str,
    ) -> None: ...

    async def broadcast_to_channels(self, jid: str, text: str) -> None: ...

    @property
    def plugin_manager(self) -> pluggy.PluginManager | None: ...


_scheduler_running = False
_cron_job_next_runs: dict[str, str] = {}


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
            await _poll_host_cron_jobs()

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

        await asyncio.sleep(get_settings().scheduler.poll_interval)


def _get_cron_job_next_run(schedule: str, timezone: str) -> str:
    """Compute next run time for a host cron job in local scheduler timezone."""
    tz = ZoneInfo(timezone)
    cron = croniter(schedule, datetime.now(tz))
    return cron.get_next(datetime).isoformat()


def _resolve_cron_job_cwd(cwd: str | None) -> str:
    """Resolve optional cron job cwd against project root."""
    project_root = get_settings().project_root
    if not cwd:
        return str(project_root)
    path = Path(cwd)
    if path.is_absolute():
        return str(path)
    return str((project_root / path).resolve())


async def _run_host_cron_job(job_name: str) -> None:
    """Run one host-level cron job command directly (no LLM/container)."""
    s = get_settings()
    job = s.cron_jobs.get(job_name)
    if job is None or not job.enabled:
        return

    command_cwd = _resolve_cron_job_cwd(job.cwd)
    logger.info(
        "Running host cron job",
        job=job_name,
        schedule=job.schedule,
        cwd=command_cwd,
    )

    try:
        process = await asyncio.create_subprocess_shell(
            job.command,
            cwd=command_cwd,
            stdout=PIPE,
            stderr=PIPE,
        )
    except Exception as exc:
        logger.error("Failed to start host cron job", job=job_name, err=str(exc))
        return

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=job.timeout_seconds,
        )
    except TimeoutError:
        logger.error(
            "Host cron job timed out",
            job=job_name,
            timeout_seconds=job.timeout_seconds,
        )
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.communicate()
        return
    except Exception as exc:
        logger.error("Host cron job failed during execution", job=job_name, err=str(exc))
        return

    stdout_text = stdout.decode(errors="replace").strip()
    stderr_text = stderr.decode(errors="replace").strip()

    if process.returncode == 0:
        logger.info(
            "Host cron job completed",
            job=job_name,
            exit_code=process.returncode,
            stdout_tail=stdout_text[-500:] if stdout_text else "",
        )
    else:
        logger.error(
            "Host cron job failed",
            job=job_name,
            exit_code=process.returncode,
            stdout_tail=stdout_text[-500:] if stdout_text else "",
            stderr_tail=stderr_text[-500:] if stderr_text else "",
        )


async def _poll_host_cron_jobs() -> None:
    """Run due host cron jobs configured in settings.cron_jobs."""
    s = get_settings()
    cron_jobs = getattr(s, "cron_jobs", {})
    if not cron_jobs:
        return

    now = datetime.now(UTC)
    timezone = s.timezone

    for job_name, job in cron_jobs.items():
        if not job.enabled:
            continue

        next_run = _cron_job_next_runs.get(job_name)
        if next_run is None:
            next_run = _get_cron_job_next_run(job.schedule, timezone)
            _cron_job_next_runs[job_name] = next_run

        due_at = datetime.fromisoformat(next_run).astimezone(UTC)
        if due_at > now:
            continue

        # Set next run before execution to avoid repeat-triggering in tight loops.
        _cron_job_next_runs[job_name] = _get_cron_job_next_run(job.schedule, timezone)
        await _run_host_cron_job(job_name)


async def _run_task(task: ScheduledTask, deps: SchedulerDependencies) -> None:
    """Execute a single scheduled task."""
    start_time = datetime.now(UTC)
    s = get_settings()
    group_dir = s.groups_dir / task.group_folder
    group_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Running scheduled task", task_id=task.id, group=task.group_folder)

    groups = deps.registered_groups()
    group = next((g for g in groups.values() if g.folder == task.group_folder), None)

    if group:
        await deps.broadcast_to_channels(
            task.chat_jid,
            "⏱ Scheduled task starting.",
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
                run_at=datetime.now(UTC).isoformat(),
                duration_ms=(datetime.now(UTC) - start_time).total_seconds() * 1000,
                status="error",
                result=None,
                error=f"Group not found: {task.group_folder}",
            )
        )
        return

    _is_god = group.is_god

    # Write tasks snapshot so the container can read current task state
    all_tasks = await get_all_tasks()
    write_tasks_snapshot(
        task.group_folder,
        _is_god,
        [t.to_snapshot_dict() for t in all_tasks],
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

    def _idle_timeout_callback() -> None:
        logger.debug("Scheduled task idle timeout, closing stdin", task_id=task.id)
        deps.queue.close_stdin(task.chat_jid)

    def _reset_idle_timer() -> None:
        nonlocal idle_handle
        if idle_handle is not None:
            idle_handle.cancel()
        idle_handle = loop.call_later(s.idle_timeout, _idle_timeout_callback)

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

        agent_core_module, agent_core_class = resolve_agent_core(deps.plugin_manager)

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
            if streamed.type == "tool_use":
                tool_name = streamed.tool_name or "tool"
                tool_input = streamed.tool_input or {}
                preview = format_tool_preview(tool_name, tool_input)
                await deps.broadcast_to_channels(task.chat_jid, f"\U0001f527 {preview}")
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
            from pynchy.worktree import merge_and_push_worktree

            merge_and_push_worktree(task.group_folder)
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
    next_run = compute_next_run(task.schedule_type, task.schedule_value, s.timezone)

    result_summary = f"Error: {error}" if error else (result[:200] if result else "Completed")
    await update_task_after_run(task.id, next_run, result_summary)
