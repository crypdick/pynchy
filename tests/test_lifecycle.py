"""Lifecycle startup regression tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pluggy
import pytest
from conftest import make_settings

from pynchy.host.orchestrator import lifecycle
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.startup_readiness import StartupReadinessError
from pynchy.plugins.connections import load_connection_runtimes
from pynchy.plugins.hookspecs import PynchySpec
from pynchy.state import get_chat_history, init_test_database
from pynchy.types import WorkspaceProfile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

hookimpl = pluggy.HookimplMarker("pynchy")


class StopAfterArgumentValidationError(Exception):
    """Sentinel raised once run_app reaches its first startup phase."""


class _ConnectionRuntime:
    def __init__(self, name: str, events: list[str], *, fail: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail = fail
        self.ready = False

    async def start(self, _context) -> None:
        self.events.append(f"start:{self.name}")
        self.ready = True
        if self.fail:
            raise RuntimeError(f"failed:{self.name}")

    async def close(self) -> None:
        self.events.append(f"close:{self.name}")
        self.ready = False

    def is_ready(self) -> bool:
        return self.ready


def _connection_plugin_manager(plugin: object) -> pluggy.PluginManager:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(plugin)
    return manager


def _completed_awaitable(value: Any = None) -> Awaitable[Any]:
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


def _failed_awaitable(exc: Exception) -> Awaitable[Any]:
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_exception(exc)
    return future


def _interrupted_recovery() -> lifecycle.startup_handler.InterruptedTurnRecovery:
    return lifecycle.startup_handler.InterruptedTurnRecovery(
        turns=(),
        commit_sha="unknown",
        resume_prompt="Continuing after host restart.",
        had_deploy_continuation=False,
        deploy_revision=None,
        rolled_back=False,
        continuation_path=None,
    )


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
async def test_connection_start_failure_closes_failing_and_prior_runtimes() -> None:
    events: list[str] = []
    app = PynchyApp()
    first = _ConnectionRuntime("first", events)
    failing = _ConnectionRuntime("failing", events, fail=True)
    untouched = _ConnectionRuntime("untouched", events)
    app.connection_runtime_owner.set([first, failing, untouched])

    with pytest.raises(RuntimeError, match="failed:failing"):
        await lifecycle.start_connection_runtimes(app)

    assert events == [
        "start:first",
        "start:failing",
        "close:failing",
        "close:first",
    ]
    assert app.connection_runtime_owner.runtimes() == ()


@pytest.mark.asyncio
async def test_connection_self_partial_start_is_closed() -> None:
    events: list[str] = []
    app = PynchyApp()
    failing = _ConnectionRuntime("failing", events, fail=True)
    app.connection_runtime_owner.set([failing])

    with pytest.raises(RuntimeError, match="failed:failing"):
        await lifecycle.start_connection_runtimes(app)

    assert events == ["start:failing", "close:failing"]
    assert failing.is_ready() is False


@pytest.mark.asyncio
async def test_connection_runtime_can_wake_temporal_before_startup_is_ready(
    monkeypatch,
    tmp_path,
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    app = PynchyApp()
    app.plugin_manager = MagicMock(spec=pluggy.PluginManager)
    app.workspaces = {
        "slack:C123": WorkspaceProfile(
            jid="slack:C123",
            name="Test",
            folder="test",
            trigger="always",
        )
    }
    events: list[str] = []
    scheduler_started = asyncio.Event()
    readiness_waiter = asyncio.create_task(app.startup_readiness.wait())

    class WakeOnStartRuntime(_ConnectionRuntime):
        async def start(self, _context) -> None:
            events.append("connection:start")
            await asyncio.wait_for(scheduler_started.wait(), timeout=1)
            assert not readiness_waiter.done()
            events.append("workflow:wake")
            self.ready = True

    app.connection_runtime_owner.set([WakeOnStartRuntime("matrix", events)])

    async def wait_for_shutdown() -> None:
        await asyncio.Event().wait()

    async def start_scheduler(
        _deps: Any,
        *,
        ready: asyncio.Future[None],
    ) -> None:
        events.append("temporal:ready")
        scheduler_started.set()
        ready.set_result(None)
        await wait_for_shutdown()

    def start_http(*_args: Any, **_kwargs: Any) -> Awaitable[object]:
        assert not readiness_waiter.done()
        events.append("http:start")
        return _completed_awaitable(MagicMock())

    def confirm_startup(_recovery: object) -> Awaitable[None]:
        assert not readiness_waiter.done()
        events.append("deploy:confirm")
        return _completed_awaitable()

    def noop_phase(*_args: Any, **_kwargs: Any) -> Awaitable[Any]:
        return _completed_awaitable()

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(lifecycle, "_initialize_core", noop_phase)
    monkeypatch.setattr(lifecycle, "_setup_channels", noop_phase)
    monkeypatch.setattr(lifecycle, "_reconcile_state", lambda _app: _completed_awaitable({}))
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "prepare_interrupted_turn_recovery",
        lambda _app: _completed_awaitable(_interrupted_recovery()),
    )
    monkeypatch.setattr(lifecycle.startup_handler, "confirm_deploy_startup", confirm_startup)
    monkeypatch.setattr(lifecycle.dep_factory, "make_scheduler_deps", lambda _app: object())
    monkeypatch.setattr(lifecycle.dep_factory, "make_ipc_deps", lambda _app: object())
    monkeypatch.setattr(lifecycle.dep_factory, "make_http_deps", lambda _app: object())
    monkeypatch.setattr(lifecycle.dep_factory, "make_status_deps", lambda _app: object())
    monkeypatch.setattr(lifecycle.task_scheduler, "start_scheduler_loop", start_scheduler)
    monkeypatch.setattr(
        lifecycle.ipc_manager,
        "start_ipc_watcher",
        lambda _deps: wait_for_shutdown(),
    )
    monkeypatch.setattr(lifecycle.http_server, "start_http_server", start_http)
    monkeypatch.setattr(lifecycle.tunnel_plugins, "check_tunnels", lambda _manager: None)
    monkeypatch.setattr(lifecycle.status, "record_start_time", lambda: None)
    monkeypatch.setattr(lifecycle.startup_handler, "send_boot_notification", noop_phase)
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "dispatch_interrupted_turn_recovery",
        lambda *_args: _completed_awaitable(set()),
    )
    monkeypatch.setattr(lifecycle.startup_handler, "recover_pending_messages", noop_phase)
    monkeypatch.setattr(app, "catch_up_channels", noop_phase)
    monkeypatch.setattr(lifecycle, "start_message_loop", noop_phase)

    await lifecycle.run_app(app)
    await readiness_waiter

    assert events == [
        "temporal:ready",
        "connection:start",
        "workflow:wake",
        "http:start",
        "deploy:confirm",
    ]
    await app.subsystem_tasks.stop()


@pytest.mark.asyncio
async def test_temporal_entry_failure_blocks_connections_and_deploy_confirmation(
    monkeypatch,
    tmp_path,
) -> None:
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
    runtime_events: list[str] = []
    app.connection_runtime_owner.set([_ConnectionRuntime("matrix", runtime_events)])
    readiness_waiter = asyncio.create_task(app.startup_readiness.wait())
    confirm_startup = AsyncMock()
    start_ipc = MagicMock()

    class FailingTemporalRuntime:
        def __init__(self, _deps: Any, _scheduler_config: object) -> None:
            pass

        async def __aenter__(self) -> None:
            raise RuntimeError("temporal unavailable")

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            pass

    def noop_phase(*_args: Any, **_kwargs: Any) -> Awaitable[Any]:
        return _completed_awaitable()

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(lifecycle.task_scheduler, "get_settings", lambda: settings)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(lifecycle, "_initialize_core", noop_phase)
    monkeypatch.setattr(lifecycle, "_setup_channels", noop_phase)
    monkeypatch.setattr(lifecycle, "_reconcile_state", lambda _app: _completed_awaitable({}))
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "prepare_interrupted_turn_recovery",
        lambda _app: _completed_awaitable(_interrupted_recovery()),
    )
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "confirm_deploy_startup",
        confirm_startup,
    )
    monkeypatch.setattr(lifecycle.dep_factory, "make_scheduler_deps", lambda _app: object())
    monkeypatch.setattr(
        lifecycle.task_scheduler,
        "TemporalSchedulerRuntime",
        FailingTemporalRuntime,
    )
    monkeypatch.setattr(lifecycle.ipc_manager, "start_ipc_watcher", start_ipc)

    with pytest.raises(RuntimeError, match="temporal unavailable"):
        await lifecycle.run_app(app)
    with pytest.raises(StartupReadinessError) as readiness_error:
        await readiness_waiter

    assert isinstance(readiness_error.value.__cause__, RuntimeError)
    assert "start:matrix" not in runtime_events
    start_ipc.assert_not_called()
    confirm_startup.assert_not_awaited()


def test_connection_runtime_loader_rejects_invalid_contribution() -> None:
    class InvalidPlugin:
        @hookimpl
        def pynchy_connection_runtime(self) -> object:
            return object()

    with pytest.raises(TypeError, match="ConnectionRuntime"):
        load_connection_runtimes(_connection_plugin_manager(InvalidPlugin()))


def test_connection_runtime_loader_rejects_duplicate_names() -> None:
    class DuplicatePlugin:
        @hookimpl
        def pynchy_connection_runtime(self) -> tuple[_ConnectionRuntime, ...]:
            events: list[str] = []
            return (
                _ConnectionRuntime("duplicate", events),
                _ConnectionRuntime("duplicate", events),
            )

    with pytest.raises(ValueError, match="duplicate runtime names"):
        load_connection_runtimes(_connection_plugin_manager(DuplicatePlugin()))


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

    def fake_prepare_recovery(_app: PynchyApp) -> Awaitable[object]:
        recovery_order.append("prepare")
        return _completed_awaitable(
            MagicMock(spec=lifecycle.startup_handler.InterruptedTurnRecovery)
        )

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
    assert recovery_order == ["prepare", "start_worker", "confirm", "dispatch"]


@pytest.mark.asyncio
async def test_connection_runtime_start_failure_stays_inside_deploy_rollback_boundary(
    monkeypatch,
    tmp_path,
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    continuation_path = settings.data_dir / "deploy_continuation.json"
    continuation_path.write_text("{}")
    app = PynchyApp()
    app.workspaces = {
        "slack:C123": WorkspaceProfile(
            jid="slack:C123",
            name="Test",
            folder="test",
            trigger="always",
        )
    }
    rollback_errors: list[Exception] = []

    def noop_phase(*_args: Any, **_kwargs: Any) -> Awaitable[Any]:
        return _completed_awaitable({})

    def fail_runtime_start(*_args: Any) -> Awaitable[None]:
        return _failed_awaitable(RuntimeError("matrix runtime failed"))

    def record_rollback(_path: Any, exc: Exception) -> Awaitable[None]:
        rollback_errors.append(exc)
        return _completed_awaitable()

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(lifecycle, "_initialize_core", noop_phase)
    monkeypatch.setattr(lifecycle, "_setup_channels", noop_phase)
    monkeypatch.setattr(lifecycle, "_reconcile_state", noop_phase)
    monkeypatch.setattr(lifecycle, "_start_subsystems", fail_runtime_start)
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "prepare_interrupted_turn_recovery",
        noop_phase,
    )
    monkeypatch.setattr(lifecycle.startup_handler, "auto_rollback", record_rollback)

    with pytest.raises(RuntimeError, match="matrix runtime failed"):
        await lifecycle.run_app(app)

    assert [str(exc) for exc in rollback_errors] == ["matrix runtime failed"]


@pytest.mark.asyncio
async def test_startup_failure_releases_waiters_and_joins_subsystem_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    continuation_path = settings.data_dir / "deploy_continuation.json"
    continuation_path.write_text("{}")
    app = PynchyApp()
    app.workspaces = {
        "slack:C123": WorkspaceProfile(
            jid="slack:C123",
            name="Test",
            folder="test",
            trigger="always",
        )
    }
    rollback_errors: list[Exception] = []
    owner_cancelled = asyncio.Event()
    allow_owner_teardown = asyncio.Event()
    readiness_waiter = asyncio.create_task(app.startup_readiness.wait())

    async def subsystem_owner() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            owner_cancelled.set()
            await allow_owner_teardown.wait()

    async def fail_startup(received_app: PynchyApp) -> None:
        received_app.subsystem_tasks.add(asyncio.create_task(subsystem_owner()))
        await asyncio.sleep(0)
        raise RuntimeError("temporal unavailable")

    def record_rollback(_path: Any, exc: Exception) -> Awaitable[None]:
        rollback_errors.append(exc)
        return _completed_awaitable()

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(
        lifecycle,
        "_initialize_core",
        lambda _app: _completed_awaitable(),
    )
    monkeypatch.setattr(
        lifecycle,
        "_setup_channels",
        lambda _app: _completed_awaitable(),
    )
    monkeypatch.setattr(lifecycle, "_reconcile_state", lambda _app: _completed_awaitable({}))
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "prepare_interrupted_turn_recovery",
        lambda *_args, **_kwargs: _completed_awaitable(_interrupted_recovery()),
    )
    monkeypatch.setattr(lifecycle, "_start_subsystems", fail_startup)
    monkeypatch.setattr(lifecycle.startup_handler, "auto_rollback", record_rollback)

    run_task = asyncio.create_task(lifecycle.run_app(app))
    await owner_cancelled.wait()
    with pytest.raises(StartupReadinessError) as readiness_error:
        await readiness_waiter

    assert isinstance(readiness_error.value.__cause__, RuntimeError)
    assert rollback_errors == []
    assert not run_task.done()

    allow_owner_teardown.set()
    with pytest.raises(RuntimeError, match="temporal unavailable"):
        await run_task

    assert [str(exc) for exc in rollback_errors] == ["temporal unavailable"]


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
