"""Shared utility functions.

Small helpers used across multiple modules. Avoids duplication of common
patterns like timestamped ID generation, schedule calculations, async shell
execution, and idle timer management.
"""

from __future__ import annotations

import asyncio
import contextlib
from asyncio.subprocess import PIPE
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from croniter import croniter

from pynchy.logger import logger


def generate_message_id(prefix: str = "") -> str:
    """Generate a unique message ID using millisecond timestamp.

    Args:
        prefix: Optional prefix (e.g. "host", "tui", "sys-notice").
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

    Always returns UTC isoformat so SQLite lexicographic comparison
    against ``datetime.now(UTC).isoformat()`` works correctly in
    ``get_due_tasks()``.

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
            raise ValueError("Interval must be positive")
        return datetime.fromtimestamp(
            datetime.now(UTC).timestamp() + ms / 1000,
            tz=UTC,
        ).isoformat()

    # 'once' tasks: no next run after execution
    return None


def create_background_task(
    coro: asyncio.coroutines,  # type: ignore[type-arg]
    *,
    name: str | None = None,
) -> asyncio.Task:  # type: ignore[type-arg]
    """Create an asyncio task that logs exceptions instead of swallowing them.

    A drop-in replacement for ``asyncio.create_task`` for fire-and-forget
    work (worktree merges, container stops) where we don't await the result
    but still want failures to appear in logs.
    """
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_log_task_exception)
    return task


def _log_task_exception(task: asyncio.Task) -> None:  # type: ignore[type-arg]
    """Callback attached to background tasks — logs unhandled exceptions."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Background task failed",
            task_name=task.get_name(),
            error=str(exc),
            exc_type=type(exc).__name__,
        )


@dataclass
class ShellResult:
    """Result of an async shell command execution."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    start_error: str | None = None


async def run_shell_command(
    command: str,
    *,
    cwd: str,
    timeout_seconds: float = 600,
) -> ShellResult:
    """Run a shell command asynchronously with timeout and structured result.

    Unlike subprocess.run, this does not block the event loop.
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


def log_shell_result(
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
