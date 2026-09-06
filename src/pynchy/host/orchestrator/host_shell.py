"""Host-side shell execution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from asyncio.subprocess import PIPE, Process
from collections.abc import (
    Mapping,
)
from dataclasses import dataclass

_SHELL_TERMINATION_GRACE_SECONDS = 10
_LOGGER = logging.getLogger(__name__)


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
    """Run a shell command asynchronously with timeout and structured result."""
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
    except Exception as exc:  # noqa: BLE001  # allow: exception-handling - start_error is surfaced by the caller.
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
    context = " ".join(f"{key}={value!r}" for key, value in sorted(extra.items()))
    if result.start_error:
        _LOGGER.error(
            "failed to start command: label=%s err=%s %s",
            label,
            result.start_error,
            context,
        )
    elif result.timed_out:
        _LOGGER.error("command timed out: label=%s %s", label, context)
    elif result.returncode == 0:
        _LOGGER.info(
            "command completed: label=%s exit_code=%s stdout_tail=%r %s",
            label,
            result.returncode,
            result.stdout[-500:] if result.stdout else "",
            context,
        )
    else:
        _LOGGER.error(
            "command failed: label=%s exit_code=%s stdout_tail=%r stderr_tail=%r %s",
            label,
            result.returncode,
            result.stdout[-500:] if result.stdout else "",
            result.stderr[-500:] if result.stderr else "",
            context,
        )
