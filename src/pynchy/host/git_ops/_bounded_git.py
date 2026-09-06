"""Bounded-output Git subprocess execution."""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess  # noqa: S404 - fixed no-shell Git commands.
import time
import typing
from dataclasses import dataclass
from pathlib import Path

from pynchy.host.git_ops.utils import (
    _PROCESS_GROUP_TERMINATION_GRACE_SECONDS,
    _SUBPROCESS_TIMEOUT,
    _configured_default_cwd,
    _git_subprocess_env,
)

_READ_CHUNK_BYTES = 8 * 1024
_MAX_STDERR_BYTES = 4 * 1024


@dataclass(frozen=True)
class BoundedGitOutput:
    """Git result whose stdout capture stops one byte past a caller limit."""

    returncode: int
    stdout: str
    stderr: str
    exceeded_limit: bool


def run_git_bounded_stdout(  # noqa: PLR0912 - streaming two pipes needs interleaved limit and timeout cleanup.
    *args: str,
    max_stdout_bytes: int,
    cwd: Path | None = None,
    timeout: int = _SUBPROCESS_TIMEOUT,
    env: dict[str, str] | None = None,
) -> BoundedGitOutput:
    """Run Git while retaining no more than one byte beyond a stdout limit."""
    if max_stdout_bytes < 0:
        raise ValueError("max_stdout_bytes must not be negative")
    command = ["git", *args]
    process = subprocess.Popen(  # noqa: S603 - Git args are fixed argv from internal callers; no shell.
        command,
        cwd=str(cwd or _configured_default_cwd()),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=_git_subprocess_env(env, inherit_env=True),
    )
    stdout_pipe = typing.cast("typing.BinaryIO", process.stdout)
    stderr_pipe = typing.cast("typing.BinaryIO", process.stderr)
    stdout = bytearray()
    stderr = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(stdout_pipe, selectors.EVENT_READ, "stdout")
    selector.register(stderr_pipe, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout
    exceeded_limit = False
    timed_out = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_unread_process_group(process)
                break
            for key, _event in selector.select(remaining):
                stream = key.fileobj
                chunk = os.read(stream.fileno(), _READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(stream)
                    continue
                if key.data == "stdout":
                    capture_remaining = max_stdout_bytes + 1 - len(stdout)
                    stdout.extend(chunk[:capture_remaining])
                    if len(chunk) > capture_remaining or len(stdout) > max_stdout_bytes:
                        exceeded_limit = True
                        _terminate_unread_process_group(process)
                        break
                elif len(stderr) < _MAX_STDERR_BYTES:
                    stderr.extend(chunk[: _MAX_STDERR_BYTES - len(stderr)])
            if exceeded_limit:
                break
        if not exceeded_limit and not timed_out:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_unread_process_group(process)
    finally:
        selector.close()
        stdout_pipe.close()
        stderr_pipe.close()
    if timed_out:
        return BoundedGitOutput(
            returncode=124,
            stdout="",
            stderr=f"git command timed out after {timeout} seconds",
            exceeded_limit=False,
        )
    return BoundedGitOutput(
        returncode=process.returncode or 0,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
        exceeded_limit=exceeded_limit,
    )


def _terminate_unread_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate a bounded-output process without buffering unread pipe data."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        # macOS can reject a session-group signal after Git exits its group.
        # The direct child still needs reaping without reading its pipes.
        with contextlib.suppress(PermissionError, ProcessLookupError):
            process.terminate()
    try:
        process.wait(timeout=_PROCESS_GROUP_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            with contextlib.suppress(PermissionError, ProcessLookupError):
                process.kill()
        process.wait()
