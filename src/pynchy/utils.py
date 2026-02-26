"""Shared utility functions.

Small helpers used across multiple modules. Avoids duplication of common
patterns like timestamped ID generation, schedule calculations, async shell
execution, atomic file writing, and idle timer management.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from asyncio.subprocess import PIPE
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from croniter import croniter

from pynchy.logger import logger


def write_json_atomic(path: Path, data: Any, *, indent: int | None = None) -> None:
    """Write JSON data to a file using atomic rename (tmp → final).

    Ensures the target file is never partially written — readers either
    see the old content or the complete new content.  Used for IPC files
    watched by filesystem events and any other write where partial reads
    must be avoided.

    Creates parent directories if they don't exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=indent))
    tmp.rename(path)


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
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Create an asyncio task that logs exceptions instead of swallowing them.

    A drop-in replacement for ``asyncio.create_task`` for fire-and-forget
    work (worktree merges, container stops) where we don't await the result
    but still want failures to appear in logs.
    """
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_log_task_exception)
    return task


def _log_task_exception(task: asyncio.Task[Any]) -> None:
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
            task_name=task.get_name(),
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
