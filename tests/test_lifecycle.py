"""Lifecycle startup regression tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest
from conftest import make_settings

from pynchy.host.orchestrator import lifecycle
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.state import get_chat_history, init_test_database
from pynchy.types import WorkspaceProfile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class StopAfterArgumentValidationError(Exception):
    """Sentinel raised once run_app reaches its first startup phase."""


def _completed_awaitable(value: Any = None) -> Awaitable[Any]:
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


def _failed_awaitable(exc: Exception) -> Awaitable[Any]:
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_exception(exc)
    return future


@pytest.mark.asyncio
async def test_run_app_resolves_pynchyapp_runtime_annotation(monkeypatch):
    def stop_before_startup(app: PynchyApp) -> Awaitable[None]:
        return _failed_awaitable(StopAfterArgumentValidationError())

    monkeypatch.setattr(lifecycle, "_initialize_core", stop_before_startup)

    with pytest.raises(StopAfterArgumentValidationError):
        await lifecycle.run_app(PynchyApp())


@pytest.mark.asyncio
async def test_pynchyapp_startup_annotations_resolve() -> None:
    app = PynchyApp()

    app.session_cleared.add("admin")
    app.attach_observers([])
    await app.set_memory_provider(None)

    assert app.session_cleared == {"admin"}


@pytest.mark.asyncio
async def test_host_broadcaster_persists_through_sqlite_messages() -> None:
    await init_test_database()
    app = PynchyApp()

    await app.broadcast_host_message("group@g.us", "Status update")

    await app.broadcast_system_notice("group@g.us", "Config changed")

    history = await get_chat_history("group@g.us", limit=10)

    assert [message.sender for message in history] == ["host", "system_notice"]
    assert [message.message_type for message in history] == ["host", "user"]
    assert [message.content for message in history] == [
        "Status update",
        "[System Notice] Config changed",
    ]
    assert history[0].metadata == {"source": "host_broadcaster"}


@pytest.mark.asyncio
async def test_run_app_waits_for_signal_shutdown_cleanup(monkeypatch, tmp_path) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    app = PynchyApp()
    app.workspaces = {
        "slack:C123": WorkspaceProfile(
            jid="slack:C123",
            name="Test",
            folder="test",
            trigger="always",
        )
    }
    signal_handlers: dict[object, Callable[[], None]] = {}
    shutdown_started = asyncio.Event()
    shutdown_can_finish = asyncio.Event()
    shutdown_finished = asyncio.Event()
    recovery_order: list[str] = []

    def noop_phase(*_args: Any, **_kwargs: Any) -> Awaitable[dict[str, list[str]] | None]:
        return _completed_awaitable({})

    def fake_prepare_recovery() -> Awaitable[object]:
        recovery_order.append("prepare")
        return _completed_awaitable(object())

    def fake_start_subsystems(*_args: Any) -> Awaitable[None]:
        recovery_order.append("start_worker")
        return _completed_awaitable()

    def fake_confirm_recovery(*_args: Any) -> Awaitable[None]:
        recovery_order.append("confirm")
        return _completed_awaitable()

    def fake_dispatch_recovery(*_args: Any) -> Awaitable[set[str]]:
        recovery_order.append("dispatch")
        return _completed_awaitable(set())

    async def fake_start_message_loop(
        _deps: Any,
        shutting_down: Callable[[], bool],
    ) -> None:
        signal_handlers[lifecycle.signal.SIGTERM]()
        await shutdown_started.wait()
        assert shutting_down()

    async def fake_shutdown_app(
        received_app: PynchyApp,
        sig_name: str,
        *,
        exit_process: bool = False,
    ) -> None:
        assert received_app is app
        assert sig_name == "SIGTERM"
        assert exit_process is True
        assert received_app.begin_shutdown()
        shutdown_started.set()
        await shutdown_can_finish.wait()
        shutdown_finished.set()

    loop = asyncio.get_running_loop()

    def fake_add_signal_handler(sig: object, callback: Callable[[], None]) -> None:
        signal_handlers[sig] = callback

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(loop, "add_signal_handler", fake_add_signal_handler)
    monkeypatch.setattr(lifecycle, "_initialize_core", noop_phase)
    monkeypatch.setattr(lifecycle, "_setup_channels", noop_phase)
    monkeypatch.setattr(lifecycle, "_reconcile_state", noop_phase)
    monkeypatch.setattr(lifecycle, "_start_subsystems", fake_start_subsystems)
    monkeypatch.setattr(lifecycle.startup_handler, "send_boot_notification", noop_phase)
    monkeypatch.setattr(lifecycle.startup_handler, "recover_pending_messages", noop_phase)
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "prepare_interrupted_turn_recovery",
        fake_prepare_recovery,
    )
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "dispatch_interrupted_turn_recovery",
        fake_dispatch_recovery,
    )
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "confirm_deploy_startup",
        fake_confirm_recovery,
    )
    monkeypatch.setattr(lifecycle.startup_handler, "setup_admin_group", noop_phase)
    monkeypatch.setattr(lifecycle, "start_message_loop", fake_start_message_loop)
    monkeypatch.setattr(lifecycle, "shutdown_app", fake_shutdown_app)

    run_task = asyncio.create_task(lifecycle.run_app(app))
    await shutdown_started.wait()
    await asyncio.sleep(0)

    assert not run_task.done()
    assert not shutdown_finished.is_set()

    shutdown_can_finish.set()
    await run_task
    assert shutdown_finished.is_set()
    assert recovery_order == ["prepare", "confirm", "start_worker", "dispatch"]


@pytest.mark.asyncio
async def test_channel_history_catch_up_defers_until_temporal_runtime(monkeypatch) -> None:
    app = PynchyApp()
    start_called = False

    def fail_if_started() -> Awaitable[None]:
        nonlocal start_called
        start_called = True
        return _completed_awaitable()

    monkeypatch.setattr(app, "start_channel_reconciliation", fail_if_started)
    monkeypatch.setattr(
        "pynchy.host.orchestrator.temporal.scheduler.temporal_scheduler_runtime_active",
        lambda: False,
    )

    await app.catch_up_channels()

    assert start_called is False


@pytest.mark.asyncio
async def test_channel_history_catch_up_degrades_on_dispatch_failure(monkeypatch) -> None:
    app = PynchyApp()
    start_called = False

    def fail_to_start() -> Awaitable[None]:
        nonlocal start_called
        start_called = True
        return _failed_awaitable(TimeoutError("temporal rpc timed out"))

    monkeypatch.setattr(app, "start_channel_reconciliation", fail_to_start)
    monkeypatch.setattr(
        "pynchy.host.orchestrator.temporal.scheduler.temporal_scheduler_runtime_active",
        lambda: True,
    )

    await app.catch_up_channels()

    assert start_called is True


@pytest.mark.asyncio
async def test_shutdown_watchdog_outlasts_container_stop_budget_and_is_cancelled(
    monkeypatch,
) -> None:
    app = PynchyApp()
    app.queue = cast("Any", _RecordingQueue())
    timers: list[Any] = []

    class FakeTimer:
        def __init__(self, interval: float, callback: Callable[[], None]) -> None:
            self.interval = interval
            self.callback = callback
            self.daemon = False
            self.started = False
            self.cancelled = False
            timers.append(self)

        def start(self) -> None:
            self.started = True

        def cancel(self) -> None:
            self.cancelled = True

    def fake_stop_gateway() -> Awaitable[None]:
        return _completed_awaitable()

    monkeypatch.setattr(lifecycle.threading, "Timer", FakeTimer)
    monkeypatch.setattr("pynchy.host.container_manager.gateway.stop_gateway", fake_stop_gateway)
    monkeypatch.setattr(lifecycle.output_handler, "get_trace_batcher", lambda: None)

    await lifecycle.shutdown_app(app, "SIGTERM")

    assert len(timers) == 1
    assert timers[0].started is True
    assert timers[0].interval >= 30
    assert timers[0].cancelled is True
    assert app.queue.shutdown_called is True


@pytest.mark.asyncio
async def test_shutdown_app_exits_zero_after_cleanup_when_requested(monkeypatch) -> None:
    app = PynchyApp()
    app.queue = cast("Any", _RecordingQueue())
    exit_codes: list[int] = []

    class FakeTimer:
        def __init__(self, _interval: float, _callback: Callable[[], None]) -> None:
            self.daemon = False

        def start(self) -> None:
            return None

        def cancel(self) -> None:
            return None

    def fake_stop_gateway() -> Awaitable[None]:
        return _completed_awaitable()

    def fake_exit(code: int) -> None:
        exit_codes.append(code)

    monkeypatch.setattr(lifecycle.threading, "Timer", FakeTimer)
    monkeypatch.setattr(lifecycle.os, "_exit", fake_exit)
    monkeypatch.setattr("pynchy.host.container_manager.gateway.stop_gateway", fake_stop_gateway)
    monkeypatch.setattr(lifecycle.output_handler, "get_trace_batcher", lambda: None)

    await lifecycle.shutdown_app(app, "SIGTERM", exit_process=True)

    assert app.queue.shutdown_called is True
    assert exit_codes == [0]


class _RecordingQueue:
    def __init__(self) -> None:
        self.shutdown_called = False

    async def shutdown(self) -> None:
        self.shutdown_called = True
