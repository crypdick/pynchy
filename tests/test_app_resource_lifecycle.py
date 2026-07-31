"""Public resource-lifecycle behavior for the application composition root."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pynchy.host.orchestrator.app as app_module
from pynchy.host.orchestrator.app import PynchyApp

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class _CloseableObserver:
    name = "test-observer"

    def __init__(self) -> None:
        self.closed = False

    def subscribe(self, _event_bus: object) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _HttpRunner:
    def __init__(self) -> None:
        self.cleaned = False

    async def cleanup(self) -> None:
        self.cleaned = True


async def test_application_owns_attached_resources_through_shutdown() -> None:
    app = PynchyApp()
    observer = _CloseableObserver()
    runner = _HttpRunner()

    app.attach_observers([observer])
    app.set_http_runner(runner)

    await app.close_observers()
    await app.cleanup_http_runner()
    await app.cleanup_http_runner()

    assert observer.closed is True
    assert runner.cleaned is True


def test_application_shutdown_transition_is_idempotent() -> None:
    app = PynchyApp()

    assert app.is_shutting_down() is False
    assert app.begin_shutdown() is True
    assert app.is_shutting_down() is True
    assert app.begin_shutdown() is False


def test_application_dispatch_cursor_preserves_the_furthest_in_flight_message() -> None:
    app = PynchyApp()
    app.last_agent_timestamp["chat"] = "2026-07-28T10:00:00Z"

    app.mark_dispatched("chat", "2026-07-28T10:00:01Z")
    app.mark_dispatched("chat", "2026-07-28T10:00:00Z")

    assert app.routing_cursor("chat") == "2026-07-28T10:00:01Z"
    assert app.pop_dispatched("chat", "fallback") == "2026-07-28T10:00:01Z"
    assert app.routing_cursor("chat") == "2026-07-28T10:00:00Z"


def test_application_exposes_update_offer_git_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = PynchyApp()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        app_module,
        "get_local_head_sha",
        lambda root: calls.append(("head", root)) or "local-sha",
    )
    monkeypatch.setattr(app_module, "get_deploy_config_hash", lambda: "config-hash")
    monkeypatch.setattr(app_module, "get_head_sha", lambda: "current-sha")
    monkeypatch.setattr(
        app_module,
        "host_update_main",
        lambda root: calls.append(("update", root)) or True,
    )
    monkeypatch.setattr(app_module, "needs_deploy", lambda old, new: old != new)
    monkeypatch.setattr(app_module, "needs_container_rebuild", lambda old, new: old == new)

    assert app.get_local_head_sha(tmp_path) == "local-sha"
    assert app.get_deploy_config_hash() == "config-hash"
    assert app.current_deploy_revision() == ("current-sha", "config-hash")
    assert app.host_update_main(tmp_path) is True
    assert app.needs_deploy("old", "new") is True
    assert app.needs_container_rebuild("same", "same") is True
    assert calls == [("head", tmp_path), ("update", tmp_path)]


def test_application_refreshes_personalization_skills_through_host_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    refreshed: list[str] = []
    monkeypatch.setattr(
        app_module,
        "refresh_personalized_agent_skills",
        refreshed.append,
    )

    app.refresh_personalized_agent_skills("group")

    assert refreshed == ["group"]


def test_application_syncs_personalization_with_the_validator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = PynchyApp()
    validator = object()
    calls: list[tuple[Path, object]] = []

    def sync(root: Path, validate: object) -> str:
        calls.append((root, validate))
        return "synced"

    monkeypatch.setattr("pynchy.config.api.validate_personalization_configuration", validator)
    monkeypatch.setattr("pynchy.host.git_ops.api.sync_personalization_repo", sync)

    assert app.sync_personalization(tmp_path) == "synced"
    assert calls == [(tmp_path, validator)]
