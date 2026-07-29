"""Shutdown ordering regressions for externally managed runtime resources."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.host.orchestrator import lifecycle
from pynchy.host.orchestrator.app import PynchyApp


@pytest.mark.asyncio
async def test_shutdown_stops_gateway_before_queue_failure(monkeypatch) -> None:
    app = PynchyApp()
    events: list[str] = []
    watchdog = MagicMock()

    class FailingQueue:
        async def shutdown(self) -> None:
            events.append("queue")
            raise RuntimeError("queue shutdown failed")

    async def stop_gateway() -> None:
        events.append("gateway")
        await asyncio.sleep(0)

    async def close_resources(_app: PynchyApp) -> None:
        events.append("resources")
        await asyncio.sleep(0)

    app.queue = cast("Any", FailingQueue())
    monkeypatch.setattr(lifecycle, "_start_shutdown_watchdog", lambda: watchdog)
    monkeypatch.setattr(lifecycle, "_notify_admin_shutdown", AsyncMock())
    monkeypatch.setattr(lifecycle, "_cleanup_http_runner", AsyncMock())
    monkeypatch.setattr(lifecycle, "_close_runtime_resources", close_resources)
    monkeypatch.setattr(
        "pynchy.host.container_manager.gateway.stop_gateway",
        stop_gateway,
    )

    with pytest.raises(RuntimeError, match="queue shutdown failed"):
        await lifecycle.shutdown_app(app, "SIGTERM")

    assert events == ["gateway", "queue", "resources"]
    watchdog.cancel.assert_called_once_with()


@pytest.mark.asyncio
async def test_shutdown_closes_remaining_resources_after_gateway_failure(monkeypatch) -> None:
    app = PynchyApp()
    events: list[str] = []
    watchdog = MagicMock()

    class Queue:
        async def shutdown(self) -> None:
            events.append("queue")
            await asyncio.sleep(0)

    async def stop_gateway() -> None:
        events.append("gateway")
        await asyncio.sleep(0)
        raise RuntimeError("gateway shutdown failed")

    async def close_resources(_app: PynchyApp) -> None:
        events.append("resources")
        await asyncio.sleep(0)

    app.queue = cast("Any", Queue())
    monkeypatch.setattr(lifecycle, "_start_shutdown_watchdog", lambda: watchdog)
    monkeypatch.setattr(lifecycle, "_notify_admin_shutdown", AsyncMock())
    monkeypatch.setattr(lifecycle, "_cleanup_http_runner", AsyncMock())
    monkeypatch.setattr(lifecycle, "_close_runtime_resources", close_resources)
    monkeypatch.setattr(
        "pynchy.host.container_manager.gateway.stop_gateway",
        stop_gateway,
    )

    with pytest.raises(RuntimeError, match="gateway shutdown failed"):
        await lifecycle.shutdown_app(app, "SIGTERM")

    assert events == ["gateway", "queue", "resources"]
    watchdog.cancel.assert_called_once_with()
