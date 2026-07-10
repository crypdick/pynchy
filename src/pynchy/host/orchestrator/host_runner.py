"""Direct host execution for agent cores."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from pynchy.config import get_settings
from pynchy.host.container_manager.serialization import input_to_dict, parse_container_output
from pynchy.logger import logger
from pynchy.types import ContainerInput, ContainerOutput

OnOutput = Callable[[ContainerOutput], Awaitable[None]]

_STREAM_LINE_LIMIT = 32 * 1024 * 1024
_HOST_RUNNER_PROJECT = Path("src/pynchy/agent/agent_runner")


@runtime_checkable
class _HostRunnerStdin(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class _HostRunnerStderr(Protocol):
    async def read(self) -> bytes: ...


@runtime_checkable
class _HostRunnerProcess(Protocol):
    @property
    def stdin(self) -> _HostRunnerStdin | None: ...

    @property
    def stdout(self) -> AsyncIterator[bytes] | None: ...

    @property
    def stderr(self) -> _HostRunnerStderr | None: ...

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...
    def kill(self) -> None: ...


def _host_runner_command() -> list[str]:
    project = get_settings().project_root / _HOST_RUNNER_PROJECT
    return ["uv", "run", "--project", str(project), "python", "-m", "agent_runner.host_direct"]


def _host_runner_env(extra_env: dict[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return env


def _host_runner_payload(input_data: ContainerInput, cwd: Path) -> bytes:
    payload = {
        "cwd": str(cwd),
        "input": input_to_dict(input_data),
    }
    return json.dumps(payload).encode()


async def _write_payload(proc: _HostRunnerProcess, payload: bytes) -> None:
    if proc.stdin is None:
        raise RuntimeError("host runner subprocess missing stdin")
    proc.stdin.write(payload)
    await proc.stdin.drain()
    proc.stdin.close()


async def _read_stderr(proc: _HostRunnerProcess) -> str:
    if proc.stderr is None:
        return ""
    return (await proc.stderr.read()).decode(errors="replace")


async def _stream_outputs(proc: _HostRunnerProcess, on_output: OnOutput) -> bool:
    if proc.stdout is None:
        raise RuntimeError("host runner subprocess missing stdout")

    saw_error = False
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        output = parse_container_output(line)
        saw_error = saw_error or output.status == "error"
        await on_output(output)
    return saw_error


async def run_host_input(
    input_data: ContainerInput,
    *,
    cwd: Path,
    on_output: OnOutput,
    timeout_seconds: int | float,
    env: dict[str, str] | None = None,
) -> str:
    """Run one agent turn directly on the host via a child process."""
    proc = await asyncio.create_subprocess_exec(
        *_host_runner_command(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=_host_runner_env(env),
        limit=_STREAM_LINE_LIMIT,
    )
    await _write_payload(proc, _host_runner_payload(input_data, cwd))
    stderr_task = asyncio.create_task(_read_stderr(proc), name="host-runner-stderr")

    try:
        saw_error = await asyncio.wait_for(_stream_outputs(proc, on_output), timeout_seconds)
        return_code = await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        proc.kill()
        logger.error("Host agent runner timed out", group=input_data.group_folder)
        await on_output(ContainerOutput(status="error", error="Host agent runner timed out"))
        return "error"

    stderr_text = await stderr_task
    if stderr_text:
        logger.debug("Host agent runner stderr", group=input_data.group_folder, stderr=stderr_text)
    if return_code != 0:
        await on_output(
            ContainerOutput(
                status="error",
                error=f"Host agent runner exited with code {return_code}: {stderr_text[:500]}",
            )
        )
        return "error"
    return "error" if saw_error else "success"
