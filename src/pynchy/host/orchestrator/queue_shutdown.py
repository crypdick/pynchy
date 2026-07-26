"""Shutdown orchestration for per-thread queue processes."""

from __future__ import annotations

import asyncio

import pynchy.host.container_manager.process as container_process
import pynchy.host.container_manager.session as container_session
from pynchy.host.orchestrator.host_runner import stop_host_process
from pynchy.host.orchestrator.queue_state import (  # noqa: TC001, RUF100 - beartype resolves queue annotations at runtime.
    GroupState,
)
from pynchy.logger import logger


async def shutdown_queue_processes(
    groups: dict[str, GroupState],
    *,
    active_count: int,
) -> None:
    """Destroy durable workers and stop any remaining runner processes."""
    logger.info(
        "GroupQueue shutdown starting",
        active_groups=len(groups),
        active_count=active_count,
    )
    await container_session.destroy_all_sessions()
    active: list[tuple[asyncio.subprocess.Process, str, bool]] = []
    for state in groups.values():
        proc_alive = getattr(state.process, "returncode", None) is None
        if state.process and state.container_name and proc_alive:
            active.append((state.process, state.container_name, state.is_host_process))
    if not active:
        logger.info("GroupQueue shutdown complete (no active containers)")
        return
    logger.info(
        "GroupQueue shutting down, stopping containers",
        active_count=len(active),
        containers=[name for _, name, _ in active],
    )
    await asyncio.gather(
        *(
            stop_host_process(proc)
            if is_host_process
            else container_process.graceful_stop(proc, name)
            for proc, name, is_host_process in active
        ),
        return_exceptions=True,
    )
    logger.info("GroupQueue shutdown complete", stopped_count=len(active))
