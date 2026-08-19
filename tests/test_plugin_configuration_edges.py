"""Behavior tests for provider callbacks installed by host composition."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pluggy
import pytest
from conftest import make_settings
from temporalio.service import RPCError, RPCStatusCode

from pynchy.config.api import McpTool, McpToolConfig
from pynchy.host.orchestrator import plugin_configuration
from pynchy.host.orchestrator.temporal.workflow_control import (
    TemporalRuntimeUnavailableError,
)
from pynchy.plugins.api import HostActionCatalog
from pynchy.plugins.integrations.linear import LinearMcpPlugin
from pynchy.work_items.api import WorkItemExecution


def _manager(*plugins: tuple[str, object]) -> pluggy.PluginManager:
    manager = pluggy.PluginManager("pynchy")
    for name, plugin in plugins:
        manager.register(plugin, name=name)
    return manager


def test_composition_handles_absent_optional_plugins_and_mcp_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    settings = make_settings(
        tools={
            "reader": McpTool(
                type="mcp",
                mcp=McpToolConfig(runtime="script", command="reader", port=8475),
            )
        }
    )
    configured: dict[str, object] = {}
    monkeypatch.setattr(
        plugin_configuration,
        "configure_marketplace_health_runtime",
        lambda runtime: configured.setdefault("marketplace", runtime),
    )

    plugin_configuration.configure_computer_use_plugins(manager, settings)
    plugin_configuration.configure_observer_plugins(manager)
    plugin_configuration.configure_google_setup_plugin(manager, settings)
    plugin_configuration.configure_marketplace_health_plugin(manager, settings)

    runtime = configured["marketplace"]
    assert runtime.reader_environment("reader") is not None


def test_gog_workspace_policy_fails_closed_for_invalid_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    settings = make_settings(project_root=tmp_path)

    def invalid_workspace(_settings: object, _workspace: str) -> None:
        raise ValueError("invalid workspace")

    monkeypatch.setattr(type(settings), "resolved_workspace_config", invalid_workspace)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        plugin_configuration,
        "configure_gog_runtime",
        lambda runtime: captured.setdefault("runtime", runtime),
    )

    plugin_configuration.configure_gog_plugin(settings)

    assert captured["runtime"].workspace_enables_gog("invalid") is False


@pytest.mark.parametrize(
    "failure",
    [RPCError("unavailable", RPCStatusCode.INTERNAL, b""), TemporalRuntimeUnavailableError()],
)
def test_linear_composition_converts_scheduler_failures_to_false(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    linear = LinearMcpPlugin()
    configure = Mock(wraps=linear.configure)
    linear.configure = configure
    boot_runtime: dict[str, object] = {}
    monkeypatch.setattr(
        plugin_configuration,
        "configure_linear_boot_runtime",
        lambda runtime: boot_runtime.setdefault("runtime", runtime),
    )
    plugin_configuration.configure_linear_plugin(
        _manager(("builtin-linear", linear)), make_settings(), lambda: None
    )
    assert boot_runtime["runtime"].account_for_workspace("missing") is None
    cancel = configure.call_args.kwargs["cancel_scheduled_workflow"]

    async def fail(_workflow_id: str) -> bool:
        await asyncio.sleep(0)
        raise failure

    monkeypatch.setattr(plugin_configuration, "cancel_scheduled_agent_workflow", fail)
    assert asyncio.run(cancel("workflow-1")) is False


def test_linear_composition_preserves_successful_scheduler_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linear = LinearMcpPlugin()
    configure = Mock(wraps=linear.configure)
    linear.configure = configure
    plugin_configuration.configure_linear_plugin(
        _manager(("builtin-linear", linear)), make_settings(), lambda: None
    )
    cancel = configure.call_args.kwargs["cancel_scheduled_workflow"]
    monkeypatch.setattr(
        plugin_configuration,
        "cancel_scheduled_agent_workflow",
        AsyncMock(return_value=True),
    )

    assert asyncio.run(cancel("workflow-1")) is True


async def test_linear_terminal_retirement_callback_is_beartype_resolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure = Mock()
    monkeypatch.setattr(
        plugin_configuration,
        "configure_linear_decision_inbox_runtime",
        configure,
    )
    retire = AsyncMock()
    plugin_configuration.configure_linear_plugin(
        _manager(),
        make_settings(),
        lambda: None,
        retire_provider_execution=retire,
    )
    execution = object.__new__(WorkItemExecution)

    await configure.call_args.args[0].retire_terminal_execution(execution, "revision-1")

    retire.assert_awaited_once_with(execution, "revision-1")


async def test_linear_terminal_retirement_defaults_to_host_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure = Mock()
    monkeypatch.setattr(
        plugin_configuration,
        "configure_linear_decision_inbox_runtime",
        configure,
    )
    retire = AsyncMock()
    monkeypatch.setattr(plugin_configuration, "retire_work_item_execution", retire)
    plugin_configuration.configure_linear_plugin(_manager(), make_settings(), lambda: None)
    execution = object.__new__(WorkItemExecution)

    await configure.call_args.args[0].retire_terminal_execution(execution, "revision-1")

    retire.assert_awaited_once_with(execution)


def test_builtin_canary_registration_is_idempotent() -> None:
    catalog = HostActionCatalog(actions=())
    plugin_configuration.configure_builtin_canaries(make_settings(), catalog)
    registered = plugin_configuration.registered_canary_scenarios()

    plugin_configuration.configure_builtin_canaries(make_settings(), catalog)

    assert plugin_configuration.registered_canary_scenarios() == registered
