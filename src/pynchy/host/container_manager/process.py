"""Process management — graceful stop, container removal.

Provides:
  - is_query_done_pulse() — detects query-done events in the IPC output stream
  - graceful_stop() — stops a container gracefully, killing it if it times out
  - docker_rm_force() — async force-remove a container by name
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves this runtime annotation.
)
from dataclasses import dataclass

from pynchy.agent_protocol.api import (
    ContainerOutput,  # noqa: TC001 - beartype validates query-done output at runtime.
)
from pynchy.async_tasks import create_background_task
from pynchy.logger import logger

DEFAULT_RM_FORCE_TIMEOUT_SECONDS = 15.0
DEFAULT_RM_FORCE_KILL_WAIT_SECONDS = 2.0
DEFAULT_STOP_CLI_KILL_WAIT_SECONDS = 2.0
_APPLE_RUNTIME_REAP_WAIT_SECONDS = 2.0
_APPLE_RUNTIME_REAP_POLL_SECONDS = 0.05
_pending_cli_reapers: set[asyncio.Task[int]] = set()


@dataclass
class _ProcessRuntime:
    container_cli: str | None = None
    is_apple_runtime: bool = False
    container_is_running: Callable[[str], bool] | None = None


_runtime = _ProcessRuntime()


def configure_container_process_runtime(
    *,
    container_cli: str,
    is_apple_runtime: bool,
    container_is_running: Callable[[str], bool] | None,
) -> None:
    """Inject the selected container runtime's process operations."""
    _runtime.container_cli = container_cli
    _runtime.is_apple_runtime = is_apple_runtime
    _runtime.container_is_running = container_is_running


def _configured_container_cli() -> str:
    if _runtime.container_cli is None:
        raise RuntimeError("container process runtime has not been configured")
    return _runtime.container_cli


def _retain_cli_reaper(
    proc: asyncio.subprocess.Process,
    *,
    operation: str,
    container_name: str,
) -> None:
    """Keep ownership of a killed CLI child until its delayed exit is observed."""
    task = create_background_task(
        proc.wait(),
        name=f"reap-container-{operation}-{container_name}",
    )
    _pending_cli_reapers.add(task)
    task.add_done_callback(_pending_cli_reapers.discard)


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


async def runtime_container_running(container_name: str) -> bool:
    """Return whether the selected runtime still reports one container running."""
    container_is_running = _runtime.container_is_running
    if container_is_running is None:
        return False

    try:
        return await asyncio.to_thread(container_is_running, container_name)
    except Exception as exc:  # noqa: BLE001 - best-effort probe degrades to not running.
        logger.debug(
            "Failed to inspect runtime container state",
            container=container_name,
            err=str(exc),
        )
        return False


async def graceful_stop(proc: asyncio.subprocess.Process, container_name: str) -> None:
    """Stop container gracefully with a short timeout, killing it if it doesn't exit."""
    try:
        await _stop_container_process(proc, container_name)
    except Exception as exc:  # noqa: BLE001 - cleanup boundary; any stop failure falls back to force kill.
        logger.exception(
            "Graceful stop failed, force killing",
            container=container_name,
            error=str(exc),
        )
        proc.kill()


async def _stop_container_process(proc: asyncio.subprocess.Process, container_name: str) -> None:
    stop_proc = await asyncio.create_subprocess_exec(
        _configured_container_cli(),
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
            "Graceful stop CLI timed out, killing cleanup CLI and container",
            container=container_name,
        )
        with contextlib.suppress(ProcessLookupError):
            stop_proc.kill()
        try:
            await asyncio.wait_for(
                stop_proc.wait(),
                timeout=DEFAULT_STOP_CLI_KILL_WAIT_SECONDS,
            )
        except TimeoutError:
            logger.error(
                "Graceful stop CLI did not exit after kill",
                container=container_name,
            )
            _retain_cli_reaper(
                stop_proc,
                operation="stop",
                container_name=container_name,
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


async def _run_rm_force(
    container_name: str,
    rm_timeout_seconds: float,
    kill_wait_seconds: float,
) -> bool:
    """Run ``container rm -f``/``docker rm -f`` once, bounded by ``rm_timeout_seconds``."""
    proc = await asyncio.create_subprocess_exec(
        _configured_container_cli(),
        "rm",
        "-f",
        container_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=rm_timeout_seconds)
    except TimeoutError:
        logger.warning(
            "Container force-remove timed out, killing cleanup CLI",
            container=container_name,
        )
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=kill_wait_seconds)
        except TimeoutError:
            logger.error(
                "Container force-remove CLI did not exit after kill",
                container=container_name,
            )
            _retain_cli_reaper(
                proc,
                operation="remove",
                container_name=container_name,
            )
        return False
    else:
        return True


async def _find_apple_runtime_pids(container_name: str) -> list[int]:
    """Return Apple ``container-runtime-linux`` PIDs for one exact container."""
    if not _runtime.is_apple_runtime:
        return []

    try:
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-axo",
            "pid=,command=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.debug(
            "Failed to start process scan for Apple runtime cleanup",
            container=container_name,
            err=str(exc),
        )
        return []

    stdout, _stderr = await proc.communicate()
    runtime_marker = f"/containers/{container_name} --uuid {container_name}"
    pids: list[int] = []
    for line in stdout.decode(errors="replace").splitlines():
        pid_text, _sep, command = line.strip().partition(" ")
        if not pid_text.isdigit():
            continue
        if "container-runtime-linux" not in command:
            continue
        if runtime_marker not in command:
            continue
        pids.append(int(pid_text))
    return pids


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_pids_to_exit(pids: list[int], pids_exit_timeout_seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + pids_exit_timeout_seconds
    while loop.time() < deadline:
        if all(not _pid_exists(pid) for pid in pids):
            return True
        await asyncio.sleep(_APPLE_RUNTIME_REAP_POLL_SECONDS)
    return all(not _pid_exists(pid) for pid in pids)


def _signal_pid(pid: int, sig: signal.Signals, container_name: str) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        logger.warning(
            "Failed to signal stale Apple runtime process",
            container=container_name,
            pid=pid,
            signal=sig.name,
            err=str(exc),
        )


async def reap_apple_runtime_orphans(container_name: str) -> bool:
    """Kill orphaned Apple runtime processes left behind for stopped containers.

    Apple Container can report a container as stopped while its
    ``container-runtime-linux`` process remains alive. In that illegal state,
    ``container delete --force`` hangs and later starts may hang at
    "Starting container". Reap only the exact runtime process whose root and
    UUID both match the container being cleaned.
    """
    pids = await _find_apple_runtime_pids(container_name)
    if not pids:
        return False

    for pid in pids:
        logger.warning(
            "Terminating stale Apple runtime process",
            container=container_name,
            pid=pid,
        )
        _signal_pid(pid, signal.SIGTERM, container_name)

    if not await _wait_for_pids_to_exit(pids, _APPLE_RUNTIME_REAP_WAIT_SECONDS):
        for pid in pids:
            if not _pid_exists(pid):
                continue
            logger.warning(
                "Force-killing stale Apple runtime process",
                container=container_name,
                pid=pid,
            )
            _signal_pid(pid, signal.SIGKILL, container_name)
        await _wait_for_pids_to_exit(pids, _APPLE_RUNTIME_REAP_WAIT_SECONDS)

    return True


async def docker_rm_force(
    container_name: str,
    *,
    timeout_seconds: float = DEFAULT_RM_FORCE_TIMEOUT_SECONDS,
    retry_timeout_seconds: float = DEFAULT_RM_FORCE_KILL_WAIT_SECONDS,
) -> None:
    """Force-remove a container by name, ignoring expected errors.

    Async counterpart of :func:`_docker.remove_container` — used by the
    agent-container code paths that operate on the event loop (session
    management, one-shot container cleanup).
    """
    try:
        await _run_rm_force(container_name, timeout_seconds, retry_timeout_seconds)
        if await reap_apple_runtime_orphans(container_name):
            await _run_rm_force(container_name, retry_timeout_seconds, retry_timeout_seconds)
    except OSError as exc:
        # OSError covers FileNotFoundError (CLI missing) and other
        # process-spawn failures — expected in degraded environments.
        logger.debug("docker rm -f failed", container=container_name, err=str(exc))
