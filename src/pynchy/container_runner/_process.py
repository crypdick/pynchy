"""Process management — I/O streaming, timeout handling, graceful stop.

Provides StreamState (shared mutable state for I/O readers) and the
module-level read_stdout/read_stderr coroutines extracted from the
former nested closures in run_container_agent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pynchy.config import Settings
from pynchy.container_runner._serialization import _parse_container_output
from pynchy.logger import logger
from pynchy.runtime import get_runtime
from pynchy.types import ContainerOutput

OnOutput = Callable[[ContainerOutput], Awaitable[None]]


@dataclass
class StreamState:
    """Mutable state shared between stdout/stderr readers and timeout logic."""

    stdout_buf: str = ""
    stderr_buf: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    had_streaming_output: bool = False
    new_session_id: str | None = None
    parse_buffer: str = ""


async def read_stdout(
    stream: asyncio.StreamReader,
    state: StreamState,
    max_output_size: int,
    group_name: str,
    on_output: OnOutput | None,
    reset_timeout: Callable[[], None],
) -> None:
    """Read container stdout, accumulate with truncation, and stream-parse output markers."""
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        text = chunk.decode(errors="replace")

        # Accumulate for logging (with truncation)
        if not state.stdout_truncated:
            remaining = max_output_size - len(state.stdout_buf)
            if len(text) > remaining:
                state.stdout_buf += text[:remaining]
                state.stdout_truncated = True
                logger.warning(
                    "Container stdout truncated",
                    group=group_name,
                    size=len(state.stdout_buf),
                )
            else:
                state.stdout_buf += text

        # Stream-parse for output markers
        if on_output is not None:
            state.parse_buffer += text
            while True:
                start_idx = state.parse_buffer.find(Settings.OUTPUT_START_MARKER)
                if start_idx == -1:
                    break
                end_idx = state.parse_buffer.find(Settings.OUTPUT_END_MARKER, start_idx)
                if end_idx == -1:
                    break  # Incomplete pair, wait for more data

                json_str = state.parse_buffer[
                    start_idx + len(Settings.OUTPUT_START_MARKER) : end_idx
                ].strip()
                state.parse_buffer = state.parse_buffer[end_idx + len(Settings.OUTPUT_END_MARKER) :]

                try:
                    parsed = _parse_container_output(json_str)
                    if parsed.new_session_id:
                        state.new_session_id = parsed.new_session_id
                    state.had_streaming_output = True
                    reset_timeout()
                    await on_output(parsed)
                except Exception as exc:
                    logger.warning(
                        "Failed to parse streamed output chunk",
                        group=group_name,
                        error=str(exc),
                    )


async def read_stderr(
    stream: asyncio.StreamReader,
    state: StreamState,
    max_output_size: int,
    group_name: str,
) -> None:
    """Read container stderr, log lines, and accumulate with truncation."""
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        text = chunk.decode(errors="replace")

        lines = text.strip().splitlines()
        for line in lines:
            if line:
                logger.debug(line, container=group_name)

        if not state.stderr_truncated:
            remaining = max_output_size - len(state.stderr_buf)
            if len(text) > remaining:
                state.stderr_buf += text[:remaining]
                state.stderr_truncated = True
                logger.warning(
                    "Container stderr truncated",
                    group=group_name,
                    size=len(state.stderr_buf),
                )
            else:
                state.stderr_buf += text


async def _graceful_stop(proc: asyncio.subprocess.Process, container_name: str) -> None:
    """Stop container gracefully with 15s timeout, fallback to kill."""
    try:
        stop_proc = await asyncio.create_subprocess_exec(
            get_runtime().cli,
            "stop",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(stop_proc.wait(), timeout=15.0)
        except TimeoutError:
            logger.warning(
                "Graceful stop timed out, force killing",
                container=container_name,
            )
            proc.kill()
    except Exception as exc:
        logger.exception(
            "Graceful stop failed, force killing",
            container=container_name,
            error=str(exc),
        )
        proc.kill()
