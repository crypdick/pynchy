"""Process management — timeout handling, graceful stop, stderr reading, exit classification.

Provides:
  - is_query_done_pulse() — detects query-done events in the IPC output stream
  - read_stderr() — reads container stderr, logs lines, accumulates with truncation
  - _graceful_stop() — stops a container gracefully with fallback to kill
  - _docker_rm_force() — async force-remove a container by name
  - _wait_for_exit() — manage timeout, drain I/O, wait for container exit
  - _classify_exit() — classify exit state into final ContainerOutput
  - OnOutput type alias — callback for output events
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pynchy.logger import logger
from pynchy.runtime.runtime import get_runtime
from pynchy.types import ContainerOutput

OnOutput = Callable[[ContainerOutput], Awaitable[None]]


def is_query_done_pulse(output: ContainerOutput) -> bool:
    """Detect the session-update pulse emitted after each core.query() completes.

    The container emits ContainerOutput(status="success", result=None,
    new_session_id=<id>) when a query finishes and the container returns to
    its IPC wait loop.  This pulse signals the host that the query is done
    without the container exiting.
    """
    return (
        output.status == "success"
        and output.result is None
        and output.new_session_id is not None
        and output.error is None
    )


async def read_stderr(
    stream: asyncio.StreamReader,
    max_output_size: int,
    group_name: str,
) -> str:
    """Read container stderr, log lines, and accumulate with truncation.

    Returns the accumulated stderr buffer (possibly truncated).
    """
    buf = ""
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        text = chunk.decode(errors="replace")

        lines = text.strip().splitlines()
        for line in lines:
            if line:
                logger.debug(line, container=group_name)

        if not truncated:
            remaining = max_output_size - len(buf)
            if len(text) > remaining:
                buf += text[:remaining]
                truncated = True
                logger.warning(
                    "Container stderr truncated",
                    group=group_name,
                    size=len(buf),
                )
            else:
                buf += text

    return buf


async def _graceful_stop(proc: asyncio.subprocess.Process, container_name: str) -> None:
    """Stop container gracefully with short timeout, fallback to kill."""
    try:
        stop_proc = await asyncio.create_subprocess_exec(
            get_runtime().cli,
            "stop",
            "-t",
            "5",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(stop_proc.wait(), timeout=7.0)
        except TimeoutError:
            logger.warning(
                "Graceful stop timed out, force killing",
                container=container_name,
            )
            proc.kill()
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning(
                    "Container stop did not exit docker run, force killing",
                    container=container_name,
                )
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
    except Exception as exc:
        logger.exception(
            "Graceful stop failed, force killing",
            container=container_name,
            error=str(exc),
        )
        proc.kill()


# ---------------------------------------------------------------------------
# Container exit helpers
# ---------------------------------------------------------------------------


@dataclass
class _ExitInfo:
    """Post-exit state from a container run."""

    exit_code: int
    stderr: str
    timed_out: bool
    duration_ms: float


async def _wait_for_exit(
    proc: asyncio.subprocess.Process,
    container_name: str,
    group_name: str,
    timeout_secs: float,
    max_output_size: int,
) -> _ExitInfo:
    """Manage timeout, drain stdout/stderr, wait for exit, and clean up.

    Sets up a timer-based hard timeout that gracefully stops the container.
    Drains stdout (discarded — output comes via IPC files) and stderr
    concurrently, then waits for the process to exit.

    After exit, cancels the timeout and schedules container removal.
    """
    start_time = time.monotonic()
    loop = asyncio.get_running_loop()
    timed_out = False

    def kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        logger.error(
            "Container timeout, stopping gracefully",
            group=group_name,
            container=container_name,
        )
        asyncio.create_task(_graceful_stop(proc, container_name))

    timeout_handle = loop.call_later(timeout_secs, kill_on_timeout)

    # Drain stdout + read stderr concurrently, then wait for exit
    assert proc.stdout is not None
    assert proc.stderr is not None

    async def _drain_stdout(stream: asyncio.StreamReader) -> None:
        """Consume stdout without accumulating — output comes via IPC files."""
        while await stream.read(8192):
            pass

    stderr_buf_future = asyncio.ensure_future(read_stderr(proc.stderr, max_output_size, group_name))
    await asyncio.gather(
        _drain_stdout(proc.stdout),
        stderr_buf_future,
    )
    exit_code = await proc.wait()
    stderr_buf = stderr_buf_future.result()

    timeout_handle.cancel()

    # Schedule container removal (fire-and-forget)
    asyncio.ensure_future(_docker_rm_force(container_name))

    return _ExitInfo(
        exit_code=exit_code,
        stderr=stderr_buf,
        timed_out=timed_out,
        duration_ms=(time.monotonic() - start_time) * 1000,
    )


def _classify_exit(
    exit_info: _ExitInfo,
    outputs: list[ContainerOutput],
    group_name: str,
    container_name: str,
    config_timeout: float,
) -> ContainerOutput:
    """Classify exit status and output events into a final ContainerOutput.

    Handles four cases:
    - Timeout with output → idle cleanup (success)
    - Timeout with no output → real timeout (error)
    - Non-zero exit → error
    - Clean exit → last output event or empty success
    """
    if exit_info.timed_out:
        if outputs:
            # Had output before timeout — idle cleanup, not a real error
            logger.info(
                "Container timed out after output (idle cleanup)",
                group=group_name,
                container=container_name,
                duration_ms=exit_info.duration_ms,
            )
            last = outputs[-1]
            return ContainerOutput(
                status="success", result=None, new_session_id=last.new_session_id
            )

        logger.error(
            "Container timed out with no output",
            group=group_name,
            container=container_name,
            duration_ms=exit_info.duration_ms,
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Container timed out after {config_timeout:.0f}s",
        )

    if exit_info.exit_code != 0:
        logger.error(
            "Container exited with error",
            group=group_name,
            code=exit_info.exit_code,
            duration_ms=exit_info.duration_ms,
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Container exited with code {exit_info.exit_code}: {exit_info.stderr[-200:]}",
        )

    # Use the last output event as the final result
    if outputs:
        last = outputs[-1]
        logger.info(
            "Container completed",
            group=group_name,
            duration_ms=exit_info.duration_ms,
            output_events=len(outputs),
            new_session_id=last.new_session_id,
        )
        return last

    # Container exited successfully but produced no output files
    logger.warning(
        "Container exited successfully but produced no output",
        group=group_name,
        duration_ms=exit_info.duration_ms,
    )
    return ContainerOutput(status="success", result=None)


async def _docker_rm_force(container_name: str) -> None:
    """Force-remove a container by name, ignoring expected errors.

    Async counterpart of :func:`_docker.remove_container` — used by the
    agent-container code paths that operate on the event loop (session
    management, one-shot container cleanup).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            get_runtime().cli,
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except OSError as exc:
        # OSError covers FileNotFoundError (CLI missing) and other
        # process-spawn failures — expected in degraded environments.
        logger.debug("docker rm -f failed", container=container_name, err=str(exc))
