"""Shared utility functions.

Small helpers used across multiple modules. Avoids duplication of common
patterns like timestamped ID generation, schedule calculations, async shell
execution, atomic file writing, and idle timer management.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from asyncio.subprocess import PIPE, Process
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves these runtime annotations.
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
from typing import Any, Literal
from zoneinfo import ZoneInfo

from croniter import croniter

from pynchy.logger import logger

_INTERVAL_POSITIVE_ERROR = "Interval must be positive"
_SHELL_TERMINATION_GRACE_SECONDS = 10
_PROGRESS_HARD_TIMEOUT_MULTIPLIER = 4.0
_SAFE_HOST_ENVIRONMENT = ("HOME", "PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL")


def filtered_process_environment(
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the safe host baseline plus explicitly selected values."""
    environment = {name: os.environ[name] for name in _SAFE_HOST_ENVIRONMENT if name in os.environ}
    if extra:
        environment.update(extra)
    return environment


class ProgressTimeoutError(TimeoutError):
    """Raised when an operation exceeds its silence or hard deadline."""

    def __init__(
        self,
        reason: str,
        *,
        inactivity_timeout_seconds: float,
        hard_timeout_seconds: float,
    ) -> None:
        self.reason = reason
        self.inactivity_timeout_seconds = inactivity_timeout_seconds
        self.hard_timeout_seconds = hard_timeout_seconds
        super().__init__(
            f"operation exceeded {reason} timeout "
            f"(inactivity={inactivity_timeout_seconds}s, hard={hard_timeout_seconds}s)"
        )


async def wait_for_progress[ResultT](
    operation: Awaitable[ResultT],
    *,
    progress_event: asyncio.Event,
    inactivity_timeout_seconds: int | float,
    hard_timeout_seconds: int | float | None = None,
) -> ResultT:
    """Wait for completion while activity refreshes a bounded silence deadline.

    ``progress_event`` may be set before or during the wait. The event is
    consumed only after its corresponding refresh, avoiding clear/set lost
    wakeups. The hard deadline never moves, so noisy wedges remain bounded.

    The operation is owned by this helper and is cancelled if the waiter is
    cancelled or either deadline expires.
    """
    effective_inactivity_timeout = float(inactivity_timeout_seconds)
    effective_hard_timeout = float(
        effective_inactivity_timeout * _PROGRESS_HARD_TIMEOUT_MULTIPLIER
        if hard_timeout_seconds is None
        else hard_timeout_seconds
    )
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    inactivity_deadline = started_at + effective_inactivity_timeout
    hard_deadline = started_at + effective_hard_timeout
    operation_task = asyncio.ensure_future(operation)
    progress_task: asyncio.Task[bool] | None = None

    try:
        while True:
            if operation_task.done():
                return await operation_task

            if progress_event.is_set():
                progress_event.clear()
                inactivity_deadline = loop.time() + effective_inactivity_timeout
                continue

            now = loop.time()
            deadline = min(inactivity_deadline, hard_deadline)
            if now >= deadline:
                reason = "hard" if hard_deadline <= inactivity_deadline else "inactivity"
                raise ProgressTimeoutError(
                    reason,
                    inactivity_timeout_seconds=effective_inactivity_timeout,
                    hard_timeout_seconds=effective_hard_timeout,
                )

            progress_task = asyncio.create_task(progress_event.wait())
            done, _pending = await asyncio.wait(
                (operation_task, progress_task),
                timeout=deadline - now,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation_task in done:
                return await operation_task
            if progress_task in done:
                progress_task = None
                continue
            if progress_event.is_set():
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_task
                progress_task = None
                continue

            reason = "hard" if hard_deadline <= inactivity_deadline else "inactivity"
            raise ProgressTimeoutError(
                reason,
                inactivity_timeout_seconds=effective_inactivity_timeout,
                hard_timeout_seconds=effective_hard_timeout,
            )
    finally:
        for task in (progress_task, operation_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


def write_json_atomic(path: Path, data: object, *, indent: int | None = None) -> None:
    """Write JSON data to a file using atomic rename (tmp → final).

    Ensures the target file is never partially written — a reader sees
    either the complete existing contents or the complete updated
    contents.  Used for IPC files watched by filesystem events and any
    other write where partial reads must be avoided.

    Creates parent directories if they don't exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=indent))
    tmp.rename(path)


def generate_message_id(prefix: str = "") -> str:
    """Generate a unique message ID using millisecond timestamp.

    Args:
        prefix: Optional prefix (e.g. "host" or "sys-notice").
                When provided, the ID is ``{prefix}-{ms_timestamp}``.
                When empty, returns just the ms timestamp string.
    """
    ms = int(datetime.now(UTC).timestamp() * 1000)
    return f"{prefix}-{ms}" if prefix else str(ms)


def compute_next_run(
    schedule_type: Literal["cron", "interval", "once"],
    schedule_value: str,
    timezone: str,
) -> str | None:
    """Compute the next run ISO timestamp for a scheduled task.

    Always returns UTC isoformat so schedule snapshots and Temporal workflow
    IDs use a stable, comparable timestamp representation.

    Returns None for 'once' tasks (no recurrence) or if the input is invalid.
    Raises ValueError for invalid cron/interval values so callers can reject them.
    """
    if schedule_type == "cron":
        tz = ZoneInfo(timezone)
        cron = croniter(schedule_value, datetime.now(tz))
        return cron.get_next(datetime).astimezone(UTC).isoformat()

    if schedule_type == "interval":
        ms = int(schedule_value)
        if ms <= 0:
            raise ValueError(_INTERVAL_POSITIVE_ERROR)
        return datetime.fromtimestamp(
            datetime.now(UTC).timestamp() + ms / 1000,
            tz=UTC,
        ).isoformat()

    # 'once' tasks: no next run after execution
    return None


def create_background_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Create an asyncio task that logs exceptions instead of swallowing them.

    A drop-in replacement for ``asyncio.create_task`` for fire-and-forget
    work (worktree merges, container stops) where we don't await the result
    but still want failures to appear in logs.
    """
    task = asyncio.create_task(coro)
    if name is not None:
        task.set_name(name)
    task.add_done_callback(_log_task_exception)
    return task


def _log_task_exception(task: asyncio.Future[Any]) -> None:
    """Callback attached to background tasks — logs unhandled exceptions."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # Pass the exception to exc_info so structlog renders the full
        # traceback.  logger.exception() won't work here because we're
        # in a done-callback, not an except handler.
        logger.error(
            "Background task failed",
            task_name=task.get_name() if isinstance(task, asyncio.Task) else None,
            exc_info=exc,
        )


@dataclass
class ShellResult:
    """Result of an async shell command execution."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    start_error: str | None = None


def _signal_shell_process_group(process: Process, sig: signal.Signals) -> None:
    """Signal a shell command's isolated process group."""
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        with contextlib.suppress(ProcessLookupError):
            if sig is signal.SIGTERM:
                process.terminate()
            else:
                process.kill()


async def _terminate_shell_process_group(process: Process) -> None:
    """Let shell traps clean up, then force-kill descendants that ignore TERM."""
    _signal_shell_process_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(
            process.communicate(),
            timeout=_SHELL_TERMINATION_GRACE_SECONDS,
        )
    except TimeoutError:
        _signal_shell_process_group(process, signal.SIGKILL)
    else:
        return
    with contextlib.suppress(Exception):
        await process.communicate()


async def run_shell_command(
    command: str,
    *,
    cwd: str,
    timeout_seconds: int | float = 600,
    env: Mapping[str, str] | None = None,
) -> ShellResult:
    """Run a shell command asynchronously with timeout and structured result.

    Unlike subprocess.run, this does not block the event loop.
    """
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env={**os.environ, **env} if env is not None else None,
            stdout=PIPE,
            stderr=PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return ShellResult(returncode=None, stdout="", stderr="", start_error=str(exc))

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        await _terminate_shell_process_group(process)
        return ShellResult(returncode=None, stdout="", stderr="", timed_out=True)
    except asyncio.CancelledError:
        await _terminate_shell_process_group(process)
        raise
    except Exception as exc:  # noqa: BLE001, RUF100  # allow: exception-handling - start_error is surfaced by the caller.
        await _terminate_shell_process_group(process)
        return ShellResult(returncode=None, stdout="", stderr="", start_error=str(exc))

    return ShellResult(
        returncode=process.returncode,
        stdout=stdout.decode(errors="replace").strip(),
        stderr=stderr.decode(errors="replace").strip(),
    )


def log_shell_result(
    result: ShellResult,
    *,
    label: str,
    **extra: object,
) -> None:
    """Log the outcome of a shell command execution."""
    if result.start_error:
        logger.error("failed to start command", label=label, err=result.start_error, **extra)
    elif result.timed_out:
        logger.error("command timed out", label=label, **extra)
    elif result.returncode == 0:
        logger.info(
            "command completed",
            label=label,
            exit_code=result.returncode,
            stdout_tail=result.stdout[-500:] if result.stdout else "",
            **extra,
        )
    else:
        logger.error(
            "command failed",
            label=label,
            exit_code=result.returncode,
            stdout_tail=result.stdout[-500:] if result.stdout else "",
            stderr_tail=result.stderr[-500:] if result.stderr else "",
            **extra,
        )


class IdleTimer:
    """Resettable idle timer that fires a callback after a period of inactivity.

    Used by both the message handler and the task scheduler to close
    container stdin when no output is received for ``timeout`` seconds.
    """

    def __init__(self, timeout: float, callback: Callable[[], None]) -> None:
        self._timeout = timeout
        self._callback = callback
        self._handle: asyncio.TimerHandle | None = None
        self._loop = asyncio.get_running_loop()

    def reset(self) -> None:
        """Cancel any pending timer and start a fresh countdown."""
        if self._handle is not None:
            self._handle.cancel()
        self._handle = self._loop.call_later(self._timeout, self._callback)

    def cancel(self) -> None:
        """Cancel the timer without firing the callback."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
