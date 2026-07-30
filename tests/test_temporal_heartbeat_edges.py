"""Temporal activity heartbeat lifecycle contracts."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from pynchy.host.orchestrator.temporal.heartbeats import activity_heartbeats


async def test_activity_heartbeats_retries_after_interval_timeout() -> None:
    with (
        patch(
            "pynchy.host.orchestrator.temporal.heartbeats.activity.heartbeat",
            side_effect=[None, RuntimeError("outside activity")],
        ) as heartbeat,
        patch(
            "pynchy.host.orchestrator.temporal.heartbeats.ACTIVITY_HEARTBEAT_INTERVAL_SECONDS",
            0,
        ),
    ):
        async with activity_heartbeats("details"):
            await asyncio.sleep(0.01)

    assert heartbeat.call_count >= 2
