"""Application rollback releases all owners even when one cleanup fails."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_settings

from pynchy.host.orchestrator import lifecycle
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.startup_readiness import StartupReadinessError
from pynchy.plugins.api import ConnectionRuntime, ObserverProvider


@pytest.fixture
def owned_app(monkeypatch):
    app = PynchyApp()
    runtimes = [MagicMock(spec=ConnectionRuntime, close=AsyncMock()) for _ in range(2)]
    observers = [MagicMock(spec=ObserverProvider, close=AsyncMock()) for _ in range(2)]
    channels = [MagicMock(disconnect=AsyncMock()) for _ in range(2)]
    app.connection_runtime_owner.set(runtimes)
    app.attach_observers(observers)
    app.channels = channels
    app.cleanup_http_runner = AsyncMock()
    app.queue.shutdown = AsyncMock()
    app.subsystem_tasks.stop = AsyncMock()
    batcher = MagicMock(flush_all=AsyncMock())
    monkeypatch.setattr(lifecycle.output_handler, "get_trace_batcher", lambda: batcher)
    cleanup = {
        "tasks": app.subsystem_tasks.stop,
        "http": app.cleanup_http_runner,
        "queue": app.queue.shutdown,
        "runtime": runtimes[1].close,
        "other_runtime": runtimes[0].close,
        "observer": observers[0].close,
        "other_observer": observers[1].close,
        "traces": batcher.flush_all,
        "channel": channels[0].disconnect,
        "other_channel": channels[1].disconnect,
    }
    return app, cleanup


@pytest.mark.parametrize("phase", ["core", "channels", "admin", "cancelled_core"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_early_startup_failure_retires_all_owners(
    owned_app, monkeypatch, tmp_path, phase, cleanup_fails
):
    app, cleanup = owned_app
    error = (
        asyncio.CancelledError("startup failed")
        if phase == "cancelled_core"
        else RuntimeError("startup failed")
    )
    if cleanup_fails:
        cleanup["http"].side_effect = RuntimeError("cleanup failed")
    steps = {name: AsyncMock() for name in ("core", "channels", "admin")}
    steps["core" if phase == "cancelled_core" else phase].side_effect = error
    monkeypatch.setattr(lifecycle, "get_settings", lambda: make_settings(data_dir=tmp_path))
    monkeypatch.setattr(
        lifecycle.startup_handler, "claim_deploy_continuation", lambda _: tmp_path / "absent"
    )
    monkeypatch.setattr(lifecycle, "_initialize_core", steps["core"])
    monkeypatch.setattr(lifecycle, "_setup_channels", steps["channels"])
    monkeypatch.setattr(lifecycle, "resolve_default_channel", lambda *_args: app.channels[0])
    monkeypatch.setattr(lifecycle.startup_handler, "setup_admin_group", steps["admin"])
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)
    gateway = AsyncMock()
    monkeypatch.setattr(lifecycle.gateway_manager, "stop_gateway_after_startup_failure", gateway)

    with pytest.raises(type(error), match="startup failed") as caught:
        await lifecycle.run_app(app)
    assert caught.value is error
    for close in cleanup.values():
        close.assert_awaited_once()
    gateway.assert_awaited_once()
    with pytest.raises(StartupReadinessError) as readiness:
        await asyncio.wait_for(app.startup_readiness.wait(), timeout=0.1)
    assert readiness.value.__cause__ is error


@pytest.mark.parametrize("failure", ["tasks", "http", "runtime", "observer", "channel"])
async def test_shutdown_finishes_remaining_cleanup(owned_app, monkeypatch, failure):
    app, cleanup = owned_app
    error = RuntimeError("cleanup failed")
    cleanup[failure].side_effect = error
    watchdog = MagicMock()
    gateway = AsyncMock()
    monkeypatch.setattr(lifecycle, "_start_shutdown_watchdog", lambda: watchdog)
    monkeypatch.setattr(lifecycle, "_notify_admin_shutdown", AsyncMock())
    monkeypatch.setattr(lifecycle.gateway_manager, "stop_gateway", gateway)

    with pytest.raises(RuntimeError, match="cleanup failed") as caught:
        await lifecycle.shutdown_app(app, "SIGTERM")
    assert caught.value is error
    for close in cleanup.values():
        close.assert_awaited_once()
    gateway.assert_awaited_once()
    watchdog.cancel.assert_called_once()
    assert app.connection_runtime_owner.runtimes() == ()
    await app.close_observers()
    cleanup["observer"].assert_awaited_once()


async def test_connection_rollback_preserves_start_error_and_closes_every_attempted_runtime(
    owned_app,
):
    app, cleanup = owned_app
    first, failing = app.connection_runtime_owner.runtimes()
    failing.start.side_effect = RuntimeError("start failed")
    failing.close.side_effect = RuntimeError("close failed")
    with pytest.raises(RuntimeError, match="start failed"):
        await lifecycle.start_connection_runtimes(app)
    first.close.assert_awaited_once()
    cleanup["runtime"].assert_awaited_once()
    assert app.connection_runtime_owner.runtimes() == ()
