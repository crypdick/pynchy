"""Process management — graceful stop, stderr reading, container removal.

Provides:
  - is_query_done_pulse() — detects query-done events in the IPC output stream
  - read_stderr() — reads container stderr, logs lines, accumulates with truncation
  - _graceful_stop() — stops a container gracefully with fallback to kill
  - _docker_rm_force() — async force-remove a container by name
  - OnOutput type alias — callback for output events
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

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
                with contextlib.suppress(OSError):
                    await proc.wait()
    except Exception as exc:
        logger.exception(
            "Graceful stop failed, force killing",
            container=container_name,
            error=str(exc),
        )
        proc.kill()


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
