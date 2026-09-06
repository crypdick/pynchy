"""Periodic heartbeats for long-running Temporal activities."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from temporalio import activity

ACTIVITY_HEARTBEAT_INTERVAL_SECONDS = 10
ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS = 30


def _heartbeat(details: object) -> bool:
    try:
        activity.heartbeat(details)
    except RuntimeError:
        # Unit tests call activity functions without a Temporal activity context.
        return False
    return True


@asynccontextmanager
async def activity_heartbeats(details: object) -> AsyncIterator[None]:
    """Heartbeat until the enclosed long-running activity finishes."""
    stop = asyncio.Event()

    async def _run() -> None:
        while _heartbeat(details) and not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=ACTIVITY_HEARTBEAT_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue

    heartbeat_task = asyncio.create_task(_run(), name="temporal-activity-heartbeat")
    try:
        yield
    finally:
        stop.set()
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
