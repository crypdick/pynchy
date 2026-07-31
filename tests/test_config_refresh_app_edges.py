"""Public application behavior for configuration publication rollback."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, call

import pytest

import pynchy.host.orchestrator.app as app_module
from pynchy.config.api import (
    Settings,
    load_runtime_candidate,
    publish_settings,
    repository_settings_sources,
)
from pynchy.host.orchestrator.startup_readiness import StartupReadiness
from pynchy.workspace.api import WorkspaceProfile
from tests.test_config_refresh import _ConfigRefreshApp, _write_runtime_tree

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _enable_runtime_sources(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    with repository_settings_sources(enabled=True):
        yield


async def test_failed_session_retirement_rolls_back_published_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    previous = load_runtime_candidate()
    publish_settings(previous)
    candidate = load_runtime_candidate()
    readiness = StartupReadiness()
    readiness.mark_ready()
    app = _ConfigRefreshApp(readiness, object())
    publish = Mock()
    publish_live_runtime = Mock()
    retire = AsyncMock(side_effect=RuntimeError("retirement failed"))

    monkeypatch.setattr(app_module, "publish_settings", publish)
    monkeypatch.setattr(app, "_publish_live_runtime", publish_live_runtime)
    monkeypatch.setattr(app, "_retire_runtime_policy_sessions", retire)

    with pytest.raises(RuntimeError, match="retirement failed"):
        await app.apply_config_candidate(
            candidate,
            affected_workspaces=(),
            reconcile_automations=False,
        )

    assert publish.call_args_list == [call(candidate), call(previous)]
    assert len(publish_live_runtime.call_args_list) == 2
    assert publish_live_runtime.call_args_list[0].args[0] is candidate
    assert publish_live_runtime.call_args_list[1].args[0] is previous
    retire.assert_awaited_once_with(())


async def test_failed_candidate_publication_restores_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    previous = load_runtime_candidate()
    publish_settings(previous)
    candidate = load_runtime_candidate()
    readiness = StartupReadiness()
    readiness.mark_ready()
    app = _ConfigRefreshApp(readiness, object())
    publish = Mock(side_effect=[RuntimeError("publication failed"), None])
    publish_live_runtime = Mock()

    monkeypatch.setattr(app_module, "publish_settings", publish)
    monkeypatch.setattr(app, "_publish_live_runtime", publish_live_runtime)

    with pytest.raises(RuntimeError, match="publication failed"):
        await app.apply_config_candidate(
            candidate,
            affected_workspaces=(),
            reconcile_automations=False,
        )

    assert publish.call_args_list == [call(candidate), call(previous)]
    assert len(publish_live_runtime.call_args_list) == 1
    assert publish_live_runtime.call_args_list[0].args[0] is previous


async def test_failed_session_retirement_restores_workspace_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_runtime_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    previous = load_runtime_candidate()
    publish_settings(previous)
    candidate = load_runtime_candidate()
    readiness = StartupReadiness()
    readiness.mark_ready()
    profile = WorkspaceProfile(
        jid="test@g.us",
        name="Test",
        folder="test",
        trigger="@Pynchy",
    )
    app = _ConfigRefreshApp(readiness, object())
    app.workspaces = {profile.jid: profile}
    app.queue = Mock(
        pause_runtime_policy=AsyncMock(),
        resume_runtime_policy=Mock(),
    )
    publish = Mock()
    publish_live_runtime = Mock()
    persist_profiles = AsyncMock()
    retire = AsyncMock(side_effect=RuntimeError("retirement failed"))

    monkeypatch.setattr(app_module, "publish_settings", publish)
    monkeypatch.setattr(app_module, "set_workspace_profiles", persist_profiles)
    monkeypatch.setattr(app, "_publish_live_runtime", publish_live_runtime)
    monkeypatch.setattr(app, "_retire_runtime_policy_sessions", retire)

    with pytest.raises(RuntimeError, match="retirement failed"):
        await app.apply_config_candidate(
            candidate,
            affected_workspaces=("test",),
            reconcile_automations=False,
        )

    assert persist_profiles.await_count == 2
    assert persist_profiles.await_args_list[1].args == ((profile,),)
    assert app.workspaces[profile.jid] is profile
