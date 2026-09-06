"""Lifecycle startup regression tests."""

from __future__ import annotations

import asyncio
from threading import Timer
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pluggy
import pytest
from conftest import make_settings

from pynchy.deployments import (
    DeploymentState,
    DeployRevision,
)
from pynchy.host.orchestrator import lifecycle
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.startup_readiness import StartupReadinessError
from pynchy.identifiers import SessionId
from pynchy.plugins.api import NewMessage, PynchySpec, load_connection_runtimes
from pynchy.state import (
    claim_deployment,
    get_chat_history,
    get_deployment_state,
    init_test_database,
    initialize_deployment_state,
)
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

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


def test_pynchyapp_startup_annotations_resolve() -> None:
    app = PynchyApp()

    app.session_cleared.add("admin")
    app.attach_observers([])

    assert app.session_cleared == {"admin"}


@pytest.mark.asyncio
async def test_terminal_session_clear_blocks_delayed_routed_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    folder = "project__thread_conversation-conv_terminal"
    app.session_cleared.add(folder)
    persist_session = AsyncMock()

    monkeypatch.setattr("pynchy.host.orchestrator.app.set_session", persist_session)

    await app.bind_routed_session(folder, SessionId("late-session"))

    assert app.sessions == {}
    persist_session.assert_not_awaited()


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
async def test_runtime_owner_activation_order(monkeypatch, tmp_path) -> None:
    """Commit only after handshaked owners, then publish synchronous gates."""
    app = PynchyApp()
    events: list[str] = []
    readiness_waiter = asyncio.create_task(app.startup_readiness.wait())

    class IngestingConnectionRuntime(_ConnectionRuntime):
        async def start(self, context) -> None:
            await context.ingest_message(
                "matrix:room:startup",
                NewMessage(
                    id="matrix-startup-delivery",
                    chat_jid="matrix:room:startup",
                    sender="@sender:example.com",
                    sender_name="Sender",
                    content="Pending delivery",
                    timestamp="2026-07-26T22:00:00+00:00",
                ),
            )
            await super().start(context)

    app.workspaces = {
        "slack:C123": WorkspaceProfile(
            jid="slack:C123",
            name="Test",
            folder="test",
            trigger="always",
        )
    }
    app.connection_runtime_owner.set([IngestingConnectionRuntime("matrix", events)])
    prepared = MagicMock(spec=lifecycle.http_server.PreparedHttpServer)
    recovery = _interrupted_recovery()

    def record(name: str) -> Awaitable[None]:
        events.append(name)
        return _completed_awaitable()

    def ingest_startup_delivery(
        _jid: str,
        _message: NewMessage,
    ) -> Awaitable[None]:
        if "temporal:ready" not in events:
            raise RuntimeError("Connection ingested before Temporal was ready")
        assert not readiness_waiter.done()
        events.append("connections:ingest")
        return _completed_awaitable()

    monkeypatch.setattr(app, "on_inbound", ingest_startup_delivery)
    settings = make_settings(data_dir=tmp_path)
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(lifecycle, "_initialize_core", lambda _app: record("core"))
    monkeypatch.setattr(lifecycle, "_setup_channels", lambda _app: record("channels"))
    monkeypatch.setattr(lifecycle, "_reconcile_state", lambda _app: record("state"))
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "prepare_interrupted_turn_recovery",
        lambda *_args, **_kwargs: _completed_awaitable(recovery),
    )
    monkeypatch.setattr(
        lifecycle,
        "_prepare_and_bind_control_plane",
        lambda _app: _completed_awaitable(prepared),
    )
    monkeypatch.setattr(lifecycle, "current_deploy_revision", MagicMock())
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "resolve_deploy_startup",
        lambda *_args, **_kwargs: record("deploy:resolve"),
    )
    monkeypatch.setattr(
        lifecycle.http_server,
        "recover_http_routes",
        lambda *_args: record("http:recover"),
    )
    monkeypatch.setattr(
        lifecycle,
        "_start_temporal_scheduler",
        lambda *_args: record("temporal:ready"),
    )

    async def supervise_gateway() -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(
        lifecycle.gateway_manager,
        "supervise_gateway",
        supervise_gateway,
    )
    monkeypatch.setattr(
        app,
        "start_linear_work_item_reconciliation",
        lambda: record("linear:reconcile"),
    )
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "finalize_deploy_startup",
        lambda *_args: record("deploy:finalize"),
    )
    monkeypatch.setattr(
        lifecycle.http_server,
        "publish_http_server",
        lambda *_args: events.append("http:publish"),
    )
    monkeypatch.setattr(
        lifecycle,
        "_start_ipc_watcher",
        lambda *_args: events.append("ipc"),
    )
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "send_boot_notification",
        lambda *_args: record("boot"),
    )
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "dispatch_interrupted_turn_recovery",
        lambda *_args: _completed_awaitable(set()),
    )
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "recover_pending_messages",
        lambda *_args, **_kwargs: record("messages:recover"),
    )
    monkeypatch.setattr(
        lifecycle,
        "start_message_loop",
        lambda *_args: record("message-loop"),
    )

    await lifecycle.run_app(app)
    await readiness_waiter

    assert events == [
        "core",
        "channels",
        "state",
        "temporal:ready",
        "linear:reconcile",
        "http:recover",
        "connections:ingest",
        "start:matrix",
        "deploy:resolve",
        "deploy:finalize",
        "http:publish",
        "ipc",
        "boot",
        "messages:recover",
        "message-loop",
    ]


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

    def fake_prepare_recovery(
        _app: PynchyApp,
        *,
        continuation_path: Path,
    ) -> Awaitable[object]:
        del continuation_path
        recovery_order.append("prepare")
        return _completed_awaitable(
            MagicMock(spec=lifecycle.startup_handler.InterruptedTurnRecovery)
        )

    def fake_start_runtime_owners(*_args: Any) -> Awaitable[None]:
        recovery_order.append("start_worker")
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
    monkeypatch.setattr(lifecycle, "_start_runtime_owners", fake_start_runtime_owners)
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
    assert recovery_order == ["prepare", "start_worker", "dispatch"]


@pytest.mark.asyncio
async def test_connection_runtime_start_failure_stays_inside_deploy_rollback_boundary(
    monkeypatch,
    tmp_path,
) -> None:
    await init_test_database()
    applied = DeployRevision("applied-sha", "applied-config")
    target = DeployRevision("target-sha", "target-config")
    await initialize_deployment_state(applied)
    await claim_deployment(target, force=False)
    settings = make_settings(data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    continuation_path = settings.data_dir / "deploy_continuation.json"
    continuation_path.write_text(
        '{"previous_commit_sha":"applied-sha","commit_sha":"target-sha",'
        '"config_hash":"target-config"}'
    )
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
    resolve_deployment = AsyncMock()
    prepared = MagicMock(spec=lifecycle.http_server.PreparedHttpServer)

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
    monkeypatch.setattr(
        lifecycle,
        "_prepare_and_bind_control_plane",
        lambda _app: _completed_awaitable(prepared),
    )
    monkeypatch.setattr(
        lifecycle.http_server,
        "recover_http_routes",
        lambda _prepared: _completed_awaitable(),
    )
    monkeypatch.setattr(
        lifecycle,
        "_start_temporal_scheduler",
        lambda _app: _completed_awaitable(),
    )
    monkeypatch.setattr(
        app,
        "start_linear_work_item_reconciliation",
        AsyncMock(),
    )
    monkeypatch.setattr(lifecycle, "start_connection_runtimes", fail_runtime_start)
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "resolve_deploy_startup",
        resolve_deployment,
    )
    monkeypatch.setattr(lifecycle.startup_handler, "auto_rollback", record_rollback)

    with pytest.raises(RuntimeError, match="matrix runtime failed"):
        await lifecycle.run_app(app)

    assert [str(exc) for exc in rollback_errors] == ["matrix runtime failed"]
    resolve_deployment.assert_not_awaited()
    assert await get_deployment_state() == DeploymentState(
        applied=applied,
        pending=target,
    )


@pytest.mark.asyncio
async def test_startup_failure_joins_subsystem_cleanup_before_rollback(
    monkeypatch,
    tmp_path,
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    settings.data_dir.mkdir(parents=True)
    continuation_path = settings.data_dir / "deploy_continuation.json"
    continuation_path.write_text("{}")
    app = PynchyApp()
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

    async def fail_startup(received_app: PynchyApp, *_args: object) -> None:
        received_app.subsystem_tasks.add(asyncio.create_task(subsystem_owner()))
        await asyncio.sleep(0)
        raise RuntimeError("temporal unavailable")

    def record_rollback(_path: Any, exc: Exception) -> Awaitable[None]:
        rollback_errors.append(exc)
        return _completed_awaitable()

    monkeypatch.setattr(lifecycle, "_reconcile_state", lambda _app: _completed_awaitable({}))
    monkeypatch.setattr(lifecycle, "_initialize_core", lambda _app: _completed_awaitable())
    monkeypatch.setattr(lifecycle, "_setup_channels", lambda _app: _completed_awaitable())
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "prepare_interrupted_turn_recovery",
        lambda *_args, **_kwargs: _completed_awaitable(_interrupted_recovery()),
    )
    monkeypatch.setattr(lifecycle, "_start_runtime_owners", fail_startup)
    monkeypatch.setattr(lifecycle.startup_handler, "auto_rollback", record_rollback)

    app.workspaces = {
        "slack:C123": WorkspaceProfile(
            jid="slack:C123",
            name="Test",
            folder="test",
            trigger="always",
        )
    }
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)

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

    class FakeTimer(Timer):
        def __init__(self, interval: float, callback: Callable[[], None]) -> None:
            super().__init__(interval, callback)
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

    class FakeTimer(Timer):
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
