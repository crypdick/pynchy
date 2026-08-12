"""Direct host execution for agent cores."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from pynchy.agent_protocol.api import (
    ContainerInput,
    ContainerOutput,
    input_to_dict,
    parse_container_output,
)
from pynchy.logger import logger
from pynchy.process_environment import filtered_process_environment
from pynchy.progress_wait import ProgressTimeoutError, wait_for_progress

OnOutput = Callable[[ContainerOutput], Awaitable[None]]
IsInterrupted = Callable[[], bool]

_STREAM_LINE_LIMIT = 32 * 1024 * 1024
_HOST_RUNNER_PROJECT = Path("src/pynchy/agent/agent_runner")
_STDERR_DRAIN_TIMEOUT_SECONDS = 0.1
_ERROR_DETAIL_LIMIT = 500


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

    @property
    def pid(self) -> int: ...

    async def wait(self) -> int: ...
    def kill(self) -> None: ...


OnProcessStarted = Callable[[_HostRunnerProcess], bool]


def _host_runner_command(project_root: Path) -> list[str]:
    project = project_root / _HOST_RUNNER_PROJECT
    return ["uv", "run", "--project", str(project), "python", "-m", "agent_runner.host_direct"]


def _host_runner_env(extra_env: dict[str, str] | None) -> dict[str, str]:
    return filtered_process_environment(extra_env)


def _host_runner_payload(input_data: ContainerInput, cwd: Path) -> bytes:
    payload = {
        "cwd": str(cwd),
        "input": input_to_dict(input_data),
    }
    return json.dumps(payload).encode()


async def _write_payload(proc: _HostRunnerProcess, payload: bytes) -> None:
    try:
        if proc.stdin is None:
            raise RuntimeError("host runner subprocess missing stdin")
        proc.stdin.write(payload)
        await proc.stdin.drain()
        proc.stdin.close()
    except BaseException:
        await stop_host_process(proc)
        raise


async def _read_stderr(proc: _HostRunnerProcess) -> str:
    if proc.stderr is None:
        return ""
    return (await proc.stderr.read()).decode(errors="replace")


async def _drain_stderr(stderr_task: asyncio.Task[str]) -> str:
    """Keep diagnostics from a stopped runner without waiting on a broken pipe."""
    try:
        return await asyncio.wait_for(
            asyncio.shield(stderr_task),
            timeout=_STDERR_DRAIN_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return ""


async def _stream_outputs(
    proc: _HostRunnerProcess,
    on_output: OnOutput,
    progress_event: asyncio.Event,
) -> bool:
    if proc.stdout is None:
        raise RuntimeError("host runner subprocess missing stdout")

    saw_error = False
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        output = parse_container_output(line)
        saw_error = saw_error or output.status == "error"
        # Count real structured activity before potentially slow channel delivery.
        progress_event.set()
        await on_output(output)
    return saw_error


async def stop_host_process(proc: _HostRunnerProcess) -> None:
    """Stop the host runner and its Codex child process group at a safe boundary."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        proc.kill()
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)


async def run_host_input(  # noqa: PLR0913 - direct-run contract keeps execution inputs explicit.
    input_data: ContainerInput,
    *,
    cwd: Path,
    project_root: Path,
    on_output: OnOutput,
    timeout_seconds: int | float,
    env: dict[str, str] | None = None,
    on_process_started: OnProcessStarted | None = None,
    is_interrupted: IsInterrupted | None = None,
) -> str:
    """Run one agent turn directly on the host via a child process."""
    payload = _host_runner_payload(input_data, cwd)
    proc = await asyncio.create_subprocess_exec(
        *_host_runner_command(project_root),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=_host_runner_env(env),
        limit=_STREAM_LINE_LIMIT,
        start_new_session=True,
    )
    if on_process_started is not None:
        try:
            accepted = on_process_started(proc)
        except BaseException:
            await stop_host_process(proc)
            raise
        if not accepted:
            await stop_host_process(proc)
            return "interrupted"
    await _write_payload(proc, payload)
    stderr_task = asyncio.create_task(_read_stderr(proc), name="host-runner-stderr")
    progress_event = asyncio.Event()

    try:
        saw_error = await wait_for_progress(
            _stream_outputs(proc, on_output, progress_event),
            progress_event=progress_event,
            inactivity_timeout_seconds=timeout_seconds,
        )
        return_code = await asyncio.wait_for(proc.wait(), timeout=5)
        stderr_text = await stderr_task
    except ProgressTimeoutError as exc:
        await stop_host_process(proc)
        stderr_text = await _drain_stderr(stderr_task)
        error = f"Host agent runner {exc.reason} timeout"
        if detail := stderr_text.strip()[:_ERROR_DETAIL_LIMIT]:
            error = f"{error}: {detail}"
        logger.error(
            "Host agent runner timed out",
            group=input_data.group_folder,
            timeout_reason=exc.reason,
            inactivity_timeout_seconds=exc.inactivity_timeout_seconds,
            hard_timeout_seconds=exc.hard_timeout_seconds,
            stderr=detail or None,
        )
        await on_output(
            ContainerOutput(
                status="error",
                error=error,
                query_id=input_data.query_id,
            )
        )
        return "error"
    except TimeoutError:
        await stop_host_process(proc)
        logger.error("Host agent runner did not exit", group=input_data.group_folder)
        await on_output(
            ContainerOutput(
                status="error",
                error="Host agent runner did not exit",
                query_id=input_data.query_id,
            )
        )
        return "error"
    except BaseException:
        await stop_host_process(proc)
        raise
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task

    if stderr_text:
        logger.debug("Host agent runner stderr", group=input_data.group_folder, stderr=stderr_text)
    if return_code != 0:
        if is_interrupted is not None and is_interrupted():
            return "interrupted"
        await on_output(
            ContainerOutput(
                status="error",
                error=(
                    f"Host agent runner exited with code {return_code}: "
                    f"{stderr_text[:_ERROR_DETAIL_LIMIT]}"
                ),
            )
        )
        return "error"
    return "error" if saw_error else "success"
