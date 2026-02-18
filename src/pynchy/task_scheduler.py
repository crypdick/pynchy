"""Task scheduler — runs scheduled tasks on their due dates."""

from __future__ import annotations

import asyncio
import contextlib
from asyncio.subprocess import PIPE
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from croniter import croniter

from pynchy.config import get_settings
from pynchy.db import (
    get_due_host_jobs,
    get_due_tasks,
    get_task_by_id,
    log_task_run,
    update_host_job_after_run,
    update_task_after_run,
)
from pynchy.group_queue import GroupQueue
from pynchy.logger import logger
from pynchy.types import ContainerOutput, ScheduledTask, TaskRunLog, WorkspaceProfile
from pynchy.utils import IdleTimer, compute_next_run


class SchedulerDependencies(Protocol):
    """Dependencies for the task scheduler."""

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def queue(self) -> GroupQueue: ...

    async def broadcast_to_channels(self, jid: str, text: str) -> None: ...

    async def run_agent(
        self,
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict],
        on_output: Any | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        is_scheduled_task: bool = False,
        pynchy_repo_access_override: bool | None = None,
        input_source: str = "user",
    ) -> str: ...

    async def handle_streamed_output(
        self, chat_jid: str, group: WorkspaceProfile, result: ContainerOutput
    ) -> bool: ...


_scheduler_lock = asyncio.Lock()
_scheduler_running = False
_cron_job_next_runs: dict[str, str] = {}


async def start_scheduler_loop(deps: SchedulerDependencies) -> None:
    """Start the scheduler polling loop."""
    global _scheduler_running
    async with _scheduler_lock:
        if _scheduler_running:
            logger.debug("Scheduler loop already running, skipping duplicate start")
            return
        _scheduler_running = True
    logger.info("Scheduler loop started")

    while True:
        try:
            await _poll_host_cron_jobs()

            # Only poll database host jobs if database is available
            try:
                await _poll_database_host_jobs()
            except RuntimeError as exc:
                if "Database not initialized" not in str(exc):
                    raise

            due_tasks = await get_due_tasks()
            if due_tasks:
                logger.info("Found due tasks", count=len(due_tasks))

            for task in due_tasks:
                # Re-check task status (may have been paused/cancelled)
                current_task = await get_task_by_id(task.id)
                if not current_task or current_task.status != "active":
                    continue

                async def _make_task_runner(t: ScheduledTask = current_task) -> None:
                    await _run_scheduled_agent(t, deps)

                deps.queue.enqueue_task(
                    current_task.chat_jid,
                    current_task.id,
                    _make_task_runner,
                )
        except Exception as exc:
            logger.error("Error in scheduler loop", err=str(exc))

        await asyncio.sleep(get_settings().scheduler.poll_interval)


def _get_cron_job_next_run(schedule: str, timezone: str) -> str:
    """Compute next run time for a host cron job, always in UTC."""
    tz = ZoneInfo(timezone)
    cron = croniter(schedule, datetime.now(tz))
    return cron.get_next(datetime).astimezone(UTC).isoformat()


def _resolve_cron_job_cwd(cwd: str | None) -> str:
    """Resolve optional cron job cwd against project root."""
    project_root = get_settings().project_root
    if not cwd:
        return str(project_root)
    path = Path(cwd)
    if path.is_absolute():
        return str(path)
    return str((project_root / path).resolve())


@dataclass
class ShellResult:
    """Result of a shell command execution."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    start_error: str | None = None


async def _run_shell_command(
    command: str,
    *,
    cwd: str,
    timeout_seconds: float = 600,
) -> ShellResult:
    """Run a shell command with timeout and structured result.

    Shared by cron jobs and database host jobs to avoid duplicating
    subprocess creation, timeout handling, and cleanup logic.
    """
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=PIPE,
            stderr=PIPE,
        )
    except OSError as exc:
        return ShellResult(returncode=None, stdout="", stderr="", start_error=str(exc))

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.communicate()
        return ShellResult(returncode=None, stdout="", stderr="", timed_out=True)
    except Exception as exc:
        return ShellResult(returncode=None, stdout="", stderr="", start_error=str(exc))

    return ShellResult(
        returncode=process.returncode,
        stdout=stdout.decode(errors="replace").strip(),
        stderr=stderr.decode(errors="replace").strip(),
    )


def _log_shell_result(
    result: ShellResult,
    *,
    label: str,
    **extra: Any,
) -> None:
    """Log the outcome of a shell command execution."""
    if result.start_error:
        logger.error(f"Failed to start {label}", err=result.start_error, **extra)
    elif result.timed_out:
        logger.error(f"{label} timed out", **extra)
    elif result.returncode == 0:
        logger.info(
            f"{label} completed",
            exit_code=result.returncode,
            stdout_tail=result.stdout[-500:] if result.stdout else "",
            **extra,
        )
    else:
        logger.error(
            f"{label} failed",
            exit_code=result.returncode,
            stdout_tail=result.stdout[-500:] if result.stdout else "",
            stderr_tail=result.stderr[-500:] if result.stderr else "",
            **extra,
        )


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

    result = await _run_shell_command(
        job.command,
        cwd=command_cwd,
        timeout_seconds=job.timeout_seconds,
    )
    _log_shell_result(result, label="Host cron job", job=job_name)


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


async def _poll_database_host_jobs() -> None:
    """Run due host jobs from the database (created via MCP tool)."""
    s = get_settings()
    due_jobs = await get_due_host_jobs()

    for job in due_jobs:
        logger.info(
            "Running database host job",
            job_id=job.id,
            name=job.name,
            schedule_type=job.schedule_type,
        )

        command_cwd = _resolve_cron_job_cwd(job.cwd)

        result = await _run_shell_command(
            job.command,
            cwd=command_cwd,
            timeout_seconds=job.timeout_seconds,
        )
        _log_shell_result(result, label="Database host job", job_id=job.id)

        # Calculate next run
        next_run = compute_next_run(job.schedule_type, job.schedule_value, s.timezone)
        exit_code = result.returncode if result.returncode is not None else 1
        await update_host_job_after_run(job.id, next_run, exit_code)


async def _run_scheduled_agent(task: ScheduledTask, deps: SchedulerDependencies) -> None:
    """Execute a single scheduled agent task via the unified run_agent path."""
    start_time = datetime.now(UTC)
    s = get_settings()
    group_dir = s.groups_dir / task.group_folder
    group_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Running scheduled task", task_id=task.id, group=task.group_folder)

    groups = deps.workspaces()
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

    result: str | None = None
    error: str | None = None

    # Idle timer: close container stdin after IDLE_TIMEOUT of no output,
    # so the container exits instead of hanging at waitForIpcMessage.
    def _idle_timeout_callback() -> None:
        logger.debug("Scheduled task idle timeout, closing stdin", task_id=task.id)
        deps.queue.close_stdin(task.chat_jid)

    idle_timer = IdleTimer(s.idle_timeout, _idle_timeout_callback)

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

        async def _on_output(streamed: ContainerOutput) -> None:
            nonlocal result, error
            # Delegate to the full output handler (thinking, tool_use,
            # tool_result, system, metadata, result — all broadcast).
            await deps.handle_streamed_output(task.chat_jid, group, streamed)

            if streamed.result:
                result = streamed.result
                idle_timer.reset()
            if streamed.status == "error":
                error = streamed.error or "Unknown error"

        agent_result = await deps.run_agent(
            group,
            task.chat_jid,
            task_messages,
            _on_output,
            is_scheduled_task=True,
            pynchy_repo_access_override=task.pynchy_repo_access,
            input_source="scheduled_task",
        )

        idle_timer.cancel()

        if agent_result == "error":
            error = error or "Agent returned error"

        elapsed_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
        logger.info("Task completed", task_id=task.id, duration_ms=elapsed_ms)

        # Merge worktree commits and push for all pynchy_repo_access tasks
        if not error and task.pynchy_repo_access:
            from pynchy.git_ops.worktree import merge_and_push_worktree

            await asyncio.to_thread(merge_and_push_worktree, task.group_folder)
    except Exception as exc:
        idle_timer.cancel()
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
