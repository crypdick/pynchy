"""Shared subprocess ownership for streaming CLI agent cores."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

# Tool results can occupy one large JSON line.
_STREAM_LINE_LIMIT = 32 * 1024 * 1024


def _require_stream[T](stream: T | None, name: str, core_name: str) -> T:
    if stream is None:
        raise RuntimeError(f"{core_name} subprocess missing {name} stream after creation")
    return stream


async def interrupt_process(proc: asyncio.subprocess.Process | None) -> None:
    """Allow transcript checkpointing on SIGINT, then kill and reap on timeout."""
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


@asynccontextmanager
async def cli_process(
    args: list[str],
    prompt: bytes,
    *,
    cwd: str,
    env: dict[str, str] | None,
    core_name: str,
) -> AsyncIterator[tuple[asyncio.subprocess.Process, asyncio.StreamReader, asyncio.Task[bytes]]]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        limit=_STREAM_LINE_LIMIT,
    )
    stderr_read: asyncio.Task[bytes] | None = None
    try:
        stdin = _require_stream(proc.stdin, "stdin", core_name)
        stdout = _require_stream(proc.stdout, "stdout", core_name)
        stderr = _require_stream(proc.stderr, "stderr", core_name)
        # Read both pipes concurrently so stderr cannot block the stdout producer.
        stderr_read = asyncio.create_task(stderr.read())
        stdin.write(prompt)
        await stdin.drain()
        stdin.close()
        yield proc, stdout, stderr_read
    finally:
        if stderr_read is not None:
            stderr_read.cancel()
            # A failed reader must not prevent child cleanup.
            await asyncio.gather(stderr_read, return_exceptions=True)
        # An interrupted consumer may leave stdout buffered. Drain both pipes
        # while reaping; Process.wait() alone can wait for a paused pipe forever.
        drain = asyncio.create_task(proc.communicate())
        try:
            await interrupt_process(proc)
        finally:
            await drain


async def json_objects(
    stdout: asyncio.StreamReader, log: Callable[[str], None]
) -> AsyncIterator[dict[str, Any]]:
    """Read JSON objects from a CLI stream, ignoring empty and non-JSON lines."""
    async for raw in stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log(f"skipping non-JSON stdout line: {line[:200]}")
            continue
        if isinstance(obj, dict):
            yield obj
