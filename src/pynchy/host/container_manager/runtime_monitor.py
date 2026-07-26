"""Container runtime monitoring policy and probes."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from pynchy.logger import logger
from pynchy.plugins.runtimes.detection import get_runtime


@dataclass(frozen=True)
class RuntimeMonitorPolicy:
    """Timing policy for checking a container runtime outside its CLI process."""

    poll_interval_seconds: float = 0.5
    start_grace_seconds: float = 5.0
    cli_kill_wait_seconds: float = 2.0


DEFAULT_RUNTIME_MONITOR_POLICY = RuntimeMonitorPolicy()


async def wait_for_runtime_poll_interval(interval_seconds: float) -> None:
    """Wait for the runtime poll cadence without sleep-polling inside loops."""
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    handle = loop.call_later(interval_seconds, waiter.set_result, None)
    try:
        await waiter
    finally:
        handle.cancel()


async def runtime_container_running(container_name: str) -> bool:
    """Return whether the runtime still reports the named container running."""
    if sys.platform != "darwin":
        return False

    def _check() -> bool:
        runtime = get_runtime()
        if runtime.name != "apple":
            return False
        return container_name in runtime.list_running_containers(prefix=container_name)

    try:
        return await asyncio.to_thread(_check)
    except Exception as exc:  # noqa: BLE001, RUF100 - best-effort probe degrades to not running.
        logger.debug(
            "Failed to inspect runtime container state",
            container=container_name,
            err=str(exc),
        )
        return False
