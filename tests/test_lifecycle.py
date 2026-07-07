"""Lifecycle startup regression tests."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Coroutine
from dataclasses import dataclass
from typing import Any, cast

import pluggy
import pytest
from conftest import make_settings

from pynchy.config.models import LearningConfig
from pynchy.host.learning.queue import LearningQueue
from pynchy.host.orchestrator import dep_factory, lifecycle
from pynchy.host.orchestrator.app import PynchyApp


class StopAfterArgumentValidation(Exception):
    """Sentinel raised once run_app reaches its first startup phase."""


@pytest.mark.asyncio
async def test_run_app_resolves_pynchyapp_runtime_annotation(monkeypatch):
    async def stop_before_startup(app: PynchyApp) -> None:
        raise StopAfterArgumentValidation

    monkeypatch.setattr(lifecycle, "_initialize_core", stop_before_startup)

    with pytest.raises(StopAfterArgumentValidation):
        await lifecycle.run_app(PynchyApp())


def test_make_learning_deps_uses_app_runner_and_learning_queue(monkeypatch, tmp_path) -> None:
    settings = make_settings(data_dir=tmp_path / "data")
    monkeypatch.setattr("pynchy.host.learning.queue.get_settings", lambda: settings)
    app = PynchyApp()

    deps = dep_factory.make_learning_deps(app)

    assert deps.run_agent == app.run_agent
    assert isinstance(deps.queue, LearningQueue)


@dataclass(frozen=True)
class _RecordedTask:
    name: str


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
