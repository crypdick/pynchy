"""Process management — graceful stop, container removal.

Provides:
  - is_query_done_pulse() — detects query-done events in the IPC output stream
  - _graceful_stop() — stops a container gracefully, killing it if it times out
  - _docker_rm_force() — async force-remove a container by name
  - OnOutput type alias — callback for output events
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Awaitable, Callable

from pynchy.logger import logger
from pynchy.plugins.runtimes.detection import get_runtime
from pynchy.types import ContainerOutput

OnOutput = Callable[[ContainerOutput], Awaitable[None]]

_RM_FORCE_TIMEOUT_SECONDS = 15.0
_RM_FORCE_KILL_WAIT_SECONDS = 2.0
_APPLE_RUNTIME_REAP_WAIT_SECONDS = 2.0
_APPLE_RUNTIME_REAP_POLL_SECONDS = 0.05


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


async def _graceful_stop(proc: asyncio.subprocess.Process, container_name: str) -> None:
    """Stop container gracefully with a short timeout, killing it if it doesn't exit."""
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


async def _run_rm_force(container_name: str, timeout: float) -> bool:
    """Run ``container rm -f``/``docker rm -f`` once, bounded by ``timeout``."""
    proc = await asyncio.create_subprocess_exec(
        get_runtime().cli,
        "rm",
        "-f",
        container_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        logger.warning(
            "Container force-remove timed out, killing cleanup CLI",
            container=container_name,
        )
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_RM_FORCE_KILL_WAIT_SECONDS)
        return False
    else:
        return True


async def _find_apple_runtime_pids(container_name: str) -> list[int]:
    """Return Apple ``container-runtime-linux`` PIDs for one exact container."""
    if sys.platform != "darwin":
        return []

    try:
        runtime = get_runtime()
    except RuntimeError as exc:
        logger.debug(
            "Skipping Apple runtime process scan; runtime unavailable",
            container=container_name,
            err=str(exc),
        )
        return []
    if runtime.name != "apple":
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


async def _wait_for_pids_to_exit(pids: list[int], timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
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


async def _reap_apple_runtime_orphans(container_name: str) -> bool:
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


async def _docker_rm_force(container_name: str) -> None:
    """Force-remove a container by name, ignoring expected errors.

    Async counterpart of :func:`_docker.remove_container` — used by the
    agent-container code paths that operate on the event loop (session
    management, one-shot container cleanup).
    """
    try:
        await _run_rm_force(container_name, _RM_FORCE_TIMEOUT_SECONDS)
        if await _reap_apple_runtime_orphans(container_name):
            await _run_rm_force(container_name, _RM_FORCE_KILL_WAIT_SECONDS)
    except OSError as exc:
        # OSError covers FileNotFoundError (CLI missing) and other
        # process-spawn failures — expected in degraded environments.
        logger.debug("docker rm -f failed", container=container_name, err=str(exc))
