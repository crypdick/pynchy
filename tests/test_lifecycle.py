"""Lifecycle startup regression tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, cast

import pluggy
import pytest
from conftest import make_settings

from pynchy.config.models import LearningConfig
from pynchy.host.orchestrator import lifecycle
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.types import WorkspaceProfile


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

    def noop_phase(*_args: Any, **_kwargs: Any) -> Awaitable[dict[str, list[str]] | None]:
        return _completed_awaitable({})

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
        received_app._shutting_down = True
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
    monkeypatch.setattr(lifecycle, "_start_subsystems", noop_phase)
    monkeypatch.setattr(lifecycle.startup_handler, "send_boot_notification", noop_phase)
    monkeypatch.setattr(lifecycle.startup_handler, "recover_pending_messages", noop_phase)
    monkeypatch.setattr(lifecycle.startup_handler, "check_deploy_continuation", noop_phase)
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

    await app._catch_up_channel_history()

    assert start_called is False


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


@dataclass(frozen=True)
class _RecordedTask:
    name: str


class _RecordingQueue:
    def __init__(self) -> None:
        self.shutdown_called = False

    async def shutdown(self) -> None:
        self.shutdown_called = True


class _HttpRunner:
    async def cleanup(self) -> None:
        return None


def _noop_coroutine() -> Coroutine[Any, Any, None]:
    return asyncio.sleep(0)


@pytest.mark.parametrize(
    ("enabled", "review_after_turn"),
    [
        (True, True),
        (False, True),
        (True, False),
    ],
)
@pytest.mark.asyncio
async def test_start_subsystems_does_not_start_local_learning_worker(
    monkeypatch,
    tmp_path,
    enabled: bool,
    review_after_turn: bool,
) -> None:
    settings = make_settings(
        data_dir=tmp_path / "data",
        project_root=tmp_path,
        learning=LearningConfig(enabled=enabled, review_after_turn=review_after_turn),
    )
    app = PynchyApp()
    app.plugin_manager = pluggy.PluginManager("pynchy-test")
    created_task_names: list[str] = []

    def fake_create_background_task(
        awaitable: Awaitable[None], *, name: str | None = None
    ) -> _RecordedTask:
        if inspect.iscoroutine(awaitable):
            cast("Coroutine[Any, Any, None]", awaitable).close()
        task = _RecordedTask(name=name or "")
        created_task_names.append(task.name)
        return task

    def fake_loop(*_args: Any, **_kwargs: Any) -> Coroutine[Any, Any, None]:
        return _noop_coroutine()

    def fake_start_http_server(*_args: Any, **_kwargs: Any) -> Awaitable[_HttpRunner]:
        return _completed_awaitable(_HttpRunner())

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(lifecycle, "create_background_task", fake_create_background_task)
    monkeypatch.setattr(
        "pynchy.host.orchestrator.task_scheduler.start_scheduler_loop",
        fake_loop,
    )
    monkeypatch.setattr("pynchy.host.container_manager.ipc.start_ipc_watcher", fake_loop)
    monkeypatch.setattr(
        "pynchy.host.orchestrator.http_server.start_http_server",
        fake_start_http_server,
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.status.record_start_time",
        lambda: None,
    )
    monkeypatch.setattr("pynchy.plugins.tunnels.check_tunnels", lambda _plugin_manager: None)
    await lifecycle._start_subsystems(app, {})

    assert created_task_names == ["scheduler", "ipc-watcher"]
    assert "learning-worker" not in created_task_names
