"""Process management — I/O streaming, timeout handling, graceful stop.

Provides StreamState (shared mutable state for I/O readers) and the
module-level read_stdout/read_stderr coroutines extracted from the
former nested closures in run_container_agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pynchy.config import Settings
from pynchy.container_runner._serialization import _parse_container_output
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


def extract_marker_outputs(parse_buffer: str, group_name: str) -> tuple[list[ContainerOutput], str]:
    """Extract complete marker-delimited outputs from a parse buffer.

    Scans for OUTPUT_START_MARKER / OUTPUT_END_MARKER pairs, parses the JSON
    between them, and returns (parsed_outputs, remaining_buffer).

    Shared by both one-shot read_stdout() and the persistent session reader.
    """
    outputs: list[ContainerOutput] = []
    while True:
        start_idx = parse_buffer.find(Settings.OUTPUT_START_MARKER)
        if start_idx == -1:
            break
        end_idx = parse_buffer.find(Settings.OUTPUT_END_MARKER, start_idx)
        if end_idx == -1:
            break  # Incomplete pair, wait for more data

        json_str = parse_buffer[start_idx + len(Settings.OUTPUT_START_MARKER) : end_idx].strip()
        parse_buffer = parse_buffer[end_idx + len(Settings.OUTPUT_END_MARKER) :]

        try:
            parsed = _parse_container_output(json_str)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(
                "Failed to parse streamed output chunk",
                group=group_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue

        outputs.append(parsed)
    return outputs, parse_buffer


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
    # Timing: set by orchestrator at spawn, used to measure time-to-first-output
    spawn_time: float = 0.0
    first_chunk_logged: bool = False


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

        # Log time-to-first-output (measures container boot + SDK init)
        if not state.first_chunk_logged and state.spawn_time > 0:
            elapsed_ms = (time.monotonic() - state.spawn_time) * 1000
            state.first_chunk_logged = True
            logger.info(
                "Container first stdout",
                group=group_name,
                elapsed_ms=round(elapsed_ms),
            )

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
            outputs, state.parse_buffer = extract_marker_outputs(state.parse_buffer, group_name)
            for parsed in outputs:
                if parsed.new_session_id:
                    state.new_session_id = parsed.new_session_id
                state.had_streaming_output = True
                reset_timeout()

                try:
                    await on_output(parsed)
                except Exception as exc:
                    logger.error(
                        "Output callback failed",
                        group=group_name,
                        error_type=type(exc).__name__,
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
