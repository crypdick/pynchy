"""Behavioral coverage for the lifecycle's core and channel gates."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pluggy
import pytest
from conftest import make_settings

from pynchy.config.api import WhatsAppConnectionConfig
from pynchy.host.container_manager.mcp.approval import McpApprovalRequest
from pynchy.host.container_manager.security.approval import (
    configure_approval_state_root,
    read_pending_approval,
)
from pynchy.host.container_manager.security.gate import create_gate
from pynchy.host.orchestrator import lifecycle
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.workspace.api import CapabilityRule, WorkspaceProfile, WorkspaceSecurity

if TYPE_CHECKING:
    from pathlib import Path


class _CoreStopError(Exception):
    """Stop public startup after the core phase has completed."""


async def test_app_presents_mcp_approval_through_shared_state(monkeypatch, tmp_path: Path):
    app = PynchyApp()
    app.workspaces = {
        "discord:channel:1": WorkspaceProfile(
            jid="discord:channel:1",
            name="Linear",
            folder="syn-117",
            trigger="always",
        )
    }
    app.broadcast_to_channels = AsyncMock()
    configure_approval_state_root(tmp_path / "approvals")
    create_gate(
        "syn-117",
        1000.0,
        WorkspaceSecurity(
            capabilities={"mcp.linear.linear_get_issue": CapabilityRule("needs_human")}
        ),
    )
    audit = AsyncMock()
    monkeypatch.setattr(
        "pynchy.host.orchestrator.app.get_conversation_control_by_thread",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("pynchy.host.orchestrator.app.record_security_event", audit)

    await app.request_mcp_approval(
        McpApprovalRequest(
            group_folder="syn-117",
            tool_name="linear_get_issue",
            request_data={"params": {"name": "linear_get_issue"}},
            request_id="request-1",
            capability_id="mcp.linear.linear_get_issue",
            reason="No explicit permission rule matched",
        )
    )

    pending = read_pending_approval(tmp_path / "approvals/syn-117/pending_approvals/request-1.json")
    assert pending["handler_type"] == "mcp_proxy"
    assert pending["capability_id"] == "mcp.linear.linear_get_issue"
    event = app.broadcast_to_channels.await_args.args[1]
    assert event.metadata["allow_remember"] is True
    audit.assert_awaited_once()


async def test_app_rejects_mcp_approval_without_registered_chat() -> None:
    app = PynchyApp()

    with pytest.raises(ValueError, match="No chat is registered"):
        await app.request_mcp_approval(
            McpApprovalRequest(
                group_folder="missing",
                tool_name="linear_get_issue",
                request_data={},
                request_id="request-1",
                capability_id="mcp.linear.linear_get_issue",
                reason="approval required",
            )
        )


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
    monkeypatch.setattr(lifecycle.plugin_configuration, "configure_startup_canaries", MagicMock())
    monkeypatch.setattr(
        lifecycle.speech_plugins, "get_speech_synthesizer", MagicMock(return_value=None)
    )
    monkeypatch.setattr(lifecycle.workspace_config, "configure_plugin_workspaces", MagicMock())
    monkeypatch.setattr(lifecycle.job_sources, "configure_plugin_jobs", MagicMock())
    monkeypatch.setattr(lifecycle.system_checks, "ensure_container_system_running", MagicMock())
    monkeypatch.setattr(lifecycle.gateway_manager, "start_gateway", AsyncMock())
    monkeypatch.setattr(lifecycle, "init_database", AsyncMock())
    monkeypatch.setattr(lifecycle, "recover_incomplete_action_intents", AsyncMock())
    monkeypatch.setattr(lifecycle, "recover_incomplete_webhook_effects", AsyncMock())
    monkeypatch.setattr(lifecycle, "initialize_deployment_state", AsyncMock())
    monkeypatch.setattr(lifecycle, "current_deploy_revision", MagicMock(return_value="revision"))
    monkeypatch.setattr(lifecycle, "attach_observers", MagicMock(return_value=[]))
    monkeypatch.setattr(app, "attach_observers", MagicMock())
    monkeypatch.setattr(app, "load_state", AsyncMock())

    with pytest.raises(_CoreStopError):
        await lifecycle.run_app(app)

    assert app.plugin_manager is plugin_manager
    lifecycle.service_installer.install_service.assert_called_once_with(tmp_path)
    lifecycle.gateway_manager.start_gateway.assert_awaited_once_with(
        plugin_manager,
        approval_fn=app.request_mcp_approval,
    )
    lifecycle.init_database.assert_awaited_once()
    app.attach_observers.assert_called_once_with([])
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


async def test_run_app_warns_when_a_channel_lacks_credentials(monkeypatch, tmp_path: Path):
    settings = make_settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        connections={"phone": WhatsAppConnectionConfig(auth_db_path=str(tmp_path / "auth.db"))},
    )
    app = PynchyApp()
    app.plugin_manager = pluggy.PluginManager("pynchy")
    app.workspaces = {
        "chat": WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="always")
    }
    app.message_loop_running = True
    channel = MagicMock()
    channel.connect = AsyncMock()

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "claim_deploy_continuation",
        lambda _: settings.data_dir / "missing-continuation.json",
    )
    monkeypatch.setattr(lifecycle, "_initialize_core", AsyncMock())
    monkeypatch.setattr(lifecycle, "load_channels", lambda _manager, _context: [channel])
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "validate_plugin_credentials",
        lambda _: ["TOKEN"],
    )
    monkeypatch.setattr(lifecycle.output_handler, "init_trace_batcher", MagicMock())
    monkeypatch.setattr(
        lifecycle,
        "_prepare_state_and_subsystems",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(lifecycle.startup_handler, "send_boot_notification", AsyncMock())
    monkeypatch.setattr(app, "catch_up_channels", AsyncMock())
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "dispatch_interrupted_turn_recovery",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(lifecycle.startup_handler, "recover_pending_messages", AsyncMock())
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)

    await lifecycle.run_app(app)
    channel.connect.assert_awaited_once_with()


async def test_shutdown_continues_when_admin_notification_fails(monkeypatch):
    app = PynchyApp()
    watchdog = MagicMock()
    app.queue.shutdown = AsyncMock()

    monkeypatch.setattr(
        lifecycle.orchestrator_adapters,
        "resolve_admin_notification_jid",
        MagicMock(side_effect=RuntimeError("notification unavailable")),
    )
    monkeypatch.setattr(
        lifecycle,
        "get_settings",
        lambda: MagicMock(notifications=MagicMock(admin_workspace="admin")),
    )
    monkeypatch.setattr(lifecycle, "_start_shutdown_watchdog", lambda: watchdog)
    subsystem_stop = AsyncMock()
    monkeypatch.setattr(app.subsystem_tasks, "stop", subsystem_stop)
    monkeypatch.setattr(lifecycle, "_cleanup_http_runner", AsyncMock())
    monkeypatch.setattr(lifecycle, "_close_runtime_resources", AsyncMock())
    monkeypatch.setattr(lifecycle.gateway_manager, "stop_gateway", AsyncMock())

    await lifecycle.shutdown_app(app, "SIGTERM")

    watchdog.cancel.assert_called_once_with()
    subsystem_stop.assert_awaited_once_with()


async def test_run_app_publishes_scheduler_and_ipc_after_runtime_gates(
    monkeypatch,
    tmp_path: Path,
):
    settings = make_settings(project_root=tmp_path, data_dir=tmp_path / "data")
    app = PynchyApp()
    app.plugin_manager = pluggy.PluginManager("pynchy")
    app.workspaces = {
        "chat": WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="always")
    }
    app.message_loop_running = True
    prepared = MagicMock(spec=lifecycle.http_server.PreparedHttpServer)
    recovery = MagicMock(spec=lifecycle.startup_handler.InterruptedTurnRecovery)
    scheduler_started = False

    async def start_scheduler(_deps, *, ready):
        nonlocal scheduler_started
        await asyncio.sleep(0)
        scheduler_started = True
        ready.set_result(None)

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "claim_deploy_continuation",
        lambda _: settings.data_dir / "missing-continuation.json",
    )
    monkeypatch.setattr(lifecycle, "_initialize_core", AsyncMock())
    monkeypatch.setattr(lifecycle, "_setup_channels", AsyncMock())
    monkeypatch.setattr(lifecycle, "_reconcile_state", AsyncMock(return_value={}))
    monkeypatch.setattr(
        lifecycle, "_prepare_and_bind_control_plane", AsyncMock(return_value=prepared)
    )
    monkeypatch.setattr(lifecycle.task_scheduler, "start_scheduler_loop", start_scheduler)
    monkeypatch.setattr(
        lifecycle.dep_factory, "make_scheduler_deps", MagicMock(return_value=MagicMock())
    )
    terminal_reconciliation = AsyncMock()
    monkeypatch.setattr(
        app,
        "start_linear_work_item_reconciliation",
        terminal_reconciliation,
    )
    monkeypatch.setattr(lifecycle.http_server, "recover_http_routes", AsyncMock())
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "prepare_interrupted_turn_recovery",
        AsyncMock(return_value=recovery),
    )
    monkeypatch.setattr(lifecycle.startup_handler, "resolve_deploy_startup", AsyncMock())
    monkeypatch.setattr(lifecycle.startup_handler, "finalize_deploy_startup", AsyncMock())
    monkeypatch.setattr(lifecycle.http_server, "publish_http_server", MagicMock())
    monkeypatch.setattr(lifecycle, "register_builtin_handlers", MagicMock())
    monkeypatch.setattr(lifecycle.dep_factory, "make_ipc_deps", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(lifecycle, "start_ipc_watcher", AsyncMock())
    monkeypatch.setattr(lifecycle.startup_handler, "send_boot_notification", AsyncMock())
    monkeypatch.setattr(lifecycle, "current_deploy_revision", MagicMock(return_value="revision"))
    monkeypatch.setattr(app, "catch_up_channels", AsyncMock())
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "dispatch_interrupted_turn_recovery",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(lifecycle.startup_handler, "recover_pending_messages", AsyncMock())
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)

    await lifecycle.run_app(app)

    assert scheduler_started
    terminal_reconciliation.assert_awaited_once_with()
    lifecycle.register_builtin_handlers.assert_called_once_with()
    lifecycle.start_ipc_watcher.assert_called_once()
    await app.startup_readiness.wait()


async def test_startup_cleanup_errors_preserve_original_failure(monkeypatch, tmp_path: Path):
    settings = make_settings(project_root=tmp_path, data_dir=tmp_path / "data")
    app = PynchyApp()
    app.workspaces = {
        "chat": WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="always")
    }
    startup_error = RuntimeError("runtime owner failed")

    async def fail_runtime(_app: PynchyApp, _recovery: object) -> None:
        await asyncio.sleep(0)
        raise startup_error

    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(lifecycle, "_initialize_core", AsyncMock())
    monkeypatch.setattr(lifecycle, "_setup_channels", AsyncMock())
    monkeypatch.setattr(lifecycle, "_reconcile_state", AsyncMock(return_value={}))
    monkeypatch.setattr(
        lifecycle.startup_handler,
        "prepare_interrupted_turn_recovery",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(lifecycle, "_start_runtime_owners", fail_runtime)
    monkeypatch.setattr(app, "cleanup_http_runner", AsyncMock(side_effect=OSError("http cleanup")))
    monkeypatch.setattr(
        app.connection_runtime_owner,
        "close",
        AsyncMock(side_effect=OSError("connection cleanup")),
    )

    with pytest.raises(RuntimeError, match="runtime owner failed"):
        await lifecycle.run_app(app)
