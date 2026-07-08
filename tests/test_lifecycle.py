"""Lifecycle startup regression tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock

import pluggy
import pytest
from conftest import make_settings

from pynchy.config.models import LearningConfig
from pynchy.host.learning.queue import LearningQueue
from pynchy.host.orchestrator import dep_factory, lifecycle
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.types import WorkspaceProfile


class StopAfterArgumentValidation(Exception):
    """Sentinel raised once run_app reaches its first startup phase."""


@pytest.mark.asyncio
async def test_run_app_resolves_pynchyapp_runtime_annotation(monkeypatch):
    async def stop_before_startup(app: PynchyApp) -> None:
        raise StopAfterArgumentValidation

    monkeypatch.setattr(lifecycle, "_initialize_core", stop_before_startup)

    with pytest.raises(StopAfterArgumentValidation):
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

    async def noop_phase(*_args: Any, **_kwargs: Any) -> dict[str, list[str]] | None:
        return {}

    async def fake_start_message_loop(
        _deps: Any,
        shutting_down: Callable[[], bool],
    ) -> None:
        signal_handlers[lifecycle.signal.SIGTERM]()
        await shutdown_started.wait()
        assert shutting_down()

    async def fake_shutdown_app(received_app: PynchyApp, sig_name: str) -> None:
        assert received_app is app
        assert sig_name == "SIGTERM"
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
async def test_shutdown_watchdog_outlasts_container_stop_budget_and_is_cancelled(
    monkeypatch,
) -> None:
    app = PynchyApp()
    app.queue = cast(Any, _RecordingQueue())
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

    async def fake_stop_gateway() -> None:
        return None

    monkeypatch.setattr(lifecycle.threading, "Timer", FakeTimer)
    monkeypatch.setattr("pynchy.host.container_manager.gateway.stop_gateway", fake_stop_gateway)
    monkeypatch.setattr(lifecycle.output_handler, "get_trace_batcher", lambda: None)

    await lifecycle.shutdown_app(app, "SIGTERM")

    assert len(timers) == 1
    assert timers[0].started is True
    assert timers[0].interval >= 30
    assert timers[0].cancelled is True
    assert app.queue.shutdown_called is True


def test_make_learning_deps_uses_learning_queue(monkeypatch, tmp_path) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    monkeypatch.setattr("pynchy.host.learning.queue.get_settings", lambda: settings)
    app = PynchyApp()

    deps = dep_factory.make_learning_deps(app)

    assert isinstance(deps.queue, LearningQueue)


@dataclass(frozen=True)
class _QueuedAgentTask:
    group_jid: str
    task_id: str
    fn: Callable[[], Awaitable[None]]


class _RecordingTaskQueue:
    def __init__(self, *, accepts_tasks: bool = True) -> None:
        self._accepts_tasks = accepts_tasks
        self.enqueued: list[_QueuedAgentTask] = []

    def enqueue_task(
        self,
        group_jid: str,
        task_id: str,
        fn: Callable[[], Awaitable[None]],
    ) -> bool:
        if not self._accepts_tasks:
            return False
        self.enqueued.append(_QueuedAgentTask(group_jid=group_jid, task_id=task_id, fn=fn))
        return True


@pytest.mark.asyncio
async def test_make_learning_deps_queues_learning_reviewer_run_before_running_agent(
    monkeypatch,
    tmp_path,
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    monkeypatch.setattr("pynchy.host.learning.queue.get_settings", lambda: settings)
    app = PynchyApp()
    queue = _RecordingTaskQueue()
    app.queue = cast(Any, queue)
    run_agent_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_run_agent(*args: Any, **kwargs: Any) -> str:
        run_agent_calls.append((args, kwargs))
        return "success"

    monkeypatch.setattr(app, "run_agent", fake_run_agent)
    deps = dep_factory.make_learning_deps(app)
    group = WorkspaceProfile(
        jid="learning-review:deep-work",
        name="Learning Reviewer",
        folder="learning-review-deep-work",
        trigger="",
        is_admin=False,
    )

    result_task = asyncio.create_task(
        deps.run_agent(
            group,
            "learning-review:deep-work",
            [{"role": "user", "content": "review this turn"}],
            is_scheduled_task=True,
        )
    )
    await asyncio.sleep(0)

    assert run_agent_calls == []
    assert result_task.done() is False
    assert len(queue.enqueued) == 1
    queued = queue.enqueued[0]
    assert queued.group_jid == "learning-review:deep-work"
    assert queued.task_id.startswith("learning-review-")

    await queued.fn()

    assert await result_task == "success"
    assert len(run_agent_calls) == 1
    args, kwargs = run_agent_calls[0]
    assert args[:3] == (
        group,
        "learning-review:deep-work",
        [{"role": "user", "content": "review this turn"}],
    )
    assert kwargs["is_scheduled_task"] is True


@pytest.mark.asyncio
async def test_make_learning_deps_cancels_when_queue_rejects_reviewer_run(
    monkeypatch,
    tmp_path,
) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    monkeypatch.setattr("pynchy.host.learning.queue.get_settings", lambda: settings)
    app = PynchyApp()
    queue = _RecordingTaskQueue(accepts_tasks=False)
    app.queue = cast(Any, queue)
    run_agent_mock = AsyncMock(return_value="success")
    monkeypatch.setattr(app, "run_agent", run_agent_mock)
    deps = dep_factory.make_learning_deps(app)
    group = WorkspaceProfile(
        jid="learning-review:deep-work",
        name="Learning Reviewer",
        folder="learning-review-deep-work",
        trigger="",
        is_admin=False,
    )

    with pytest.raises(asyncio.CancelledError):
        await deps.run_agent(
            group,
            "learning-review:deep-work",
            [{"role": "user", "content": "review this turn"}],
            is_scheduled_task=True,
        )

    assert queue.enqueued == []
    run_agent_mock.assert_not_awaited()


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
    async def _noop() -> None:
        return None

    return _noop()


@pytest.mark.parametrize(
    ("enabled", "review_after_turn", "expected_learning_worker"),
    [
        (True, True, True),
        (False, True, False),
        (True, False, False),
    ],
)
@pytest.mark.asyncio
async def test_start_subsystems_starts_learning_worker_only_when_after_turn_enabled(
    monkeypatch,
    tmp_path,
    enabled: bool,
    review_after_turn: bool,
    expected_learning_worker: bool,
) -> None:
    settings = make_settings(
        data_dir=tmp_path / "data",
        project_root=tmp_path,
        learning=LearningConfig(enabled=enabled, review_after_turn=review_after_turn),
    )
    app = PynchyApp()
    app.plugin_manager = pluggy.PluginManager("pynchy-test")
    created_task_names: list[str] = []
    learning_deps = object()
    make_learning_deps_calls: list[PynchyApp] = []
    learning_loop_calls: list[object] = []

    def fake_create_background_task(
        awaitable: Awaitable[None], *, name: str | None = None
    ) -> _RecordedTask:
        if inspect.iscoroutine(awaitable):
            cast(Coroutine[Any, Any, None], awaitable).close()
        task = _RecordedTask(name=name or "")
        created_task_names.append(task.name)
        return task

    def fake_loop(*_args: Any, **_kwargs: Any) -> Coroutine[Any, Any, None]:
        return _noop_coroutine()

    def fake_make_learning_deps(received_app: PynchyApp) -> object:
        make_learning_deps_calls.append(received_app)
        return learning_deps

    def fake_start_learning_worker_loop(deps: object) -> Coroutine[Any, Any, None]:
        learning_loop_calls.append(deps)
        return _noop_coroutine()

    async def fake_start_http_server(*_args: Any, **_kwargs: Any) -> _HttpRunner:
        return _HttpRunner()

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(lifecycle, "create_background_task", fake_create_background_task)
    monkeypatch.setattr(
        "pynchy.host.orchestrator.task_scheduler.start_scheduler_loop",
        fake_loop,
    )
    monkeypatch.setattr("pynchy.host.container_manager.ipc.start_ipc_watcher", fake_loop)
    monkeypatch.setattr("pynchy.host.git_ops.sync_poll.start_host_git_sync_loop", fake_loop)
    monkeypatch.setattr(
        "pynchy.host.git_ops.sync_poll.start_external_repo_sync_loop",
        fake_loop,
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.http_server.start_http_server",
        fake_start_http_server,
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.status.record_start_time",
        lambda: None,
    )
    monkeypatch.setattr("pynchy.plugins.tunnels.check_tunnels", lambda _plugin_manager: None)
    monkeypatch.setattr(
        "pynchy.host.orchestrator.dep_factory.make_learning_deps",
        fake_make_learning_deps,
        raising=False,
    )
    monkeypatch.setattr(
        "pynchy.host.learning.worker.start_learning_worker_loop",
        fake_start_learning_worker_loop,
    )

    await lifecycle._start_subsystems(app, {})

    expected_task_names = ["scheduler", "ipc-watcher", "git-sync"]
    if expected_learning_worker:
        expected_task_names.append("learning-worker")
    assert created_task_names == expected_task_names
    assert ("learning-worker" in created_task_names) is expected_learning_worker
    assert make_learning_deps_calls == ([app] if expected_learning_worker else [])
    assert learning_loop_calls == ([learning_deps] if expected_learning_worker else [])
