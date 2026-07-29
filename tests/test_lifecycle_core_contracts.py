"""Behavioral coverage for the lifecycle's core and channel gates."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pluggy
import pytest
from conftest import make_settings

from pynchy.config.api import WhatsAppConnectionConfig
from pynchy.host.orchestrator import lifecycle
from pynchy.host.orchestrator.app import PynchyApp

if TYPE_CHECKING:
    from pathlib import Path


class _CoreStopError(Exception):
    """Stop public startup after the core phase has completed."""


async def test_run_app_wires_all_core_startup_dependencies(monkeypatch, tmp_path: Path):
    settings = make_settings(project_root=tmp_path, data_dir=tmp_path / "data")
    plugin_manager = pluggy.PluginManager("pynchy")
    app = PynchyApp()
    continuation = settings.data_dir / "deploy-continuation.json"

    async def stop_after_core(_app: PynchyApp) -> None:
        await asyncio.sleep(0)
        raise _CoreStopError

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "claim_deploy_continuation",
        lambda _: continuation,
    )
    monkeypatch.setattr(lifecycle, "_setup_channels", stop_after_core)
    monkeypatch.setattr(lifecycle, "get_plugin_manager", lambda _enabled: plugin_manager)
    monkeypatch.setattr(lifecycle.service_installer, "install_service", MagicMock())
    monkeypatch.setattr(
        lifecycle.plugin_configuration, "configure_computer_use_plugins", MagicMock()
    )
    monkeypatch.setattr(lifecycle.plugin_configuration, "configure_caldav_plugin", MagicMock())
    monkeypatch.setattr(lifecycle.plugin_configuration, "configure_gog_plugin", MagicMock())
    monkeypatch.setattr(
        lifecycle.plugin_configuration,
        "configure_desktop_screenshot_plugin",
        MagicMock(),
    )
    monkeypatch.setattr(lifecycle.plugin_configuration, "configure_linear_plugin", MagicMock())
    monkeypatch.setattr(lifecycle.plugin_configuration, "configure_observer_plugins", MagicMock())
    monkeypatch.setattr(
        lifecycle.plugin_configuration,
        "configure_marketplace_health_plugin",
        MagicMock(),
    )
    monkeypatch.setattr(
        lifecycle.plugin_configuration, "configure_matrix_gateway_plugin", MagicMock()
    )
    monkeypatch.setattr(
        lifecycle.plugin_configuration, "configure_google_setup_plugin", MagicMock()
    )
    monkeypatch.setattr(lifecycle.plugin_configuration, "configure_builtin_canaries", MagicMock())
    monkeypatch.setattr(lifecycle, "initialize_host_action_catalog", MagicMock())
    monkeypatch.setattr(
        lifecycle.speech_plugins, "get_speech_synthesizer", MagicMock(return_value=None)
    )
    monkeypatch.setattr(lifecycle.workspace_config, "configure_plugin_workspaces", MagicMock())
    monkeypatch.setattr(lifecycle.job_sources, "configure_plugin_jobs", MagicMock())
    monkeypatch.setattr(lifecycle.system_checks, "ensure_container_system_running", MagicMock())
    monkeypatch.setattr(lifecycle.gateway_manager, "start_gateway", AsyncMock())
    monkeypatch.setattr(lifecycle, "init_database", AsyncMock())
    monkeypatch.setattr(lifecycle, "prepare_conversation_runtime_ownership_recovery", AsyncMock())
    monkeypatch.setattr(lifecycle, "recover_incomplete_action_intents", AsyncMock())
    monkeypatch.setattr(lifecycle, "recover_incomplete_webhook_effects", AsyncMock())
    monkeypatch.setattr(lifecycle, "initialize_deployment_state", AsyncMock())
    monkeypatch.setattr(lifecycle, "current_deploy_revision", MagicMock(return_value="revision"))
    monkeypatch.setattr(lifecycle, "attach_observers", MagicMock(return_value=[]))
    monkeypatch.setattr(lifecycle, "get_memory_provider", MagicMock(return_value=None))
    monkeypatch.setattr(app, "attach_observers", MagicMock())
    monkeypatch.setattr(app, "set_memory_provider", AsyncMock())
    monkeypatch.setattr(app, "load_state", AsyncMock())

    with pytest.raises(_CoreStopError):
        await lifecycle.run_app(app)

    assert app.plugin_manager is plugin_manager
    lifecycle.service_installer.install_service.assert_called_once_with(tmp_path)
    lifecycle.gateway_manager.start_gateway.assert_awaited_once_with(plugin_manager=plugin_manager)
    lifecycle.init_database.assert_awaited_once()
    app.attach_observers.assert_called_once_with([])
    app.set_memory_provider.assert_awaited_once_with(None)
    app.load_state.assert_awaited_once_with()


async def test_run_app_rejects_channel_setup_without_core_plugin_manager(
    monkeypatch,
    tmp_path: Path,
):
    settings = make_settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        connections={"phone": WhatsAppConnectionConfig()},
    )
    app = PynchyApp()
    loop = asyncio.get_running_loop()

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "claim_deploy_continuation",
        lambda _: settings.data_dir / "missing-continuation.json",
    )
    monkeypatch.setattr(lifecycle, "_initialize_core", AsyncMock())
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)

    with pytest.raises(RuntimeError, match=r"phase 1 \(_initialize_core\)"):
        await lifecycle.run_app(app)
