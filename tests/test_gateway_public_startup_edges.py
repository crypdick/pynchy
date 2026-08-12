"""Public gateway composition and environment edge behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pluggy
import pytest
from conftest import make_settings
from pydantic import SecretStr

from pynchy.config.api import AgentConfig, GatewayConfig, McpTool, McpToolConfig
from pynchy.host.container_manager.gateway import (
    LiteLLMGateway,
    collect_plugin_mcp_servers,
    configure_gateway_runtime,
    get_settings,
    start_gateway,
    stop_gateway,
    stop_gateway_after_startup_failure,
)
from pynchy.host.container_manager.gateway_litellm import collect_litellm_yaml_environment
from pynchy.plugins.api import McpServerConfig, McpServerSpec
from pynchy.workspace.api import ServiceTrustConfig

if TYPE_CHECKING:
    from pathlib import Path


class _FakePluginManager(pluggy.PluginManager):
    def __init__(self, hook: MagicMock) -> None:
        self.hook = hook


def test_collect_plugin_servers_ignores_malformed_contributions_and_specs() -> None:
    hook = MagicMock()
    hook.pynchy_mcp_server_spec.return_value = [
        "not-a-plugin-result",
        (
            "not-a-spec",
            McpServerSpec(
                name="browser",
                config=McpServerConfig(type="script", command="run", port=9000),
                trust=ServiceTrustConfig(secret_data=False),
            ),
            McpServerSpec(
                name="notebook",
                config=McpServerConfig(type="script", command="run", port=9001),
            ),
        ),
    ]

    servers, trust_defaults = collect_plugin_mcp_servers(_FakePluginManager(hook))

    assert set(servers) == {"browser", "notebook"}
    assert trust_defaults["browser"].secret_data is False


def test_configure_gateway_runtime_publishes_composed_settings() -> None:
    settings = object()

    configure_gateway_runtime(is_apple_container=False, get_settings=lambda: settings)  # type: ignore[arg-type]
    configure_gateway_runtime(is_apple_container=True)

    assert get_settings() is settings


def test_gateway_settings_require_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pynchy.host.container_manager.gateway._get_settings", None)

    with pytest.raises(RuntimeError, match="Gateway configuration has not been composed"):
        get_settings()


@pytest.mark.asyncio
async def test_litellm_start_requires_master_key(tmp_path: Path) -> None:
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n")
    settings = make_settings(
        gateway=GatewayConfig(litellm_config=str(config), master_key=None),
        data_dir=tmp_path,
    )

    with (
        patch("pynchy.host.container_manager.gateway.get_settings", return_value=settings),
        pytest.raises(ValueError, match="GATEWAY__MASTER_KEY"),
    ):
        await start_gateway()


@pytest.mark.asyncio
async def test_litellm_start_omits_direct_models_for_unknown_agent_core(tmp_path: Path) -> None:
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n")
    settings = make_settings(
        agent=AgentConfig(default_core="custom-core", model="agent-model"),
        gateway=GatewayConfig(
            litellm_config=str(config),
            master_key=SecretStr("master-key"),
        ),
        data_dir=tmp_path,
    )

    with (
        patch("pynchy.host.container_manager.gateway.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.security.cop_client.get_cop_gateway_config",
            return_value=("cop-model", "messages"),
        ),
        patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
    ):
        gateway = await start_gateway()

    assert isinstance(gateway, LiteLLMGateway)
    assert gateway.required_models == ()


@pytest.mark.asyncio
async def test_litellm_start_syncs_plugin_mcp_servers_after_gateway_ready(tmp_path: Path) -> None:
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n")
    settings = make_settings(
        agent=AgentConfig(default_core="codex", model="agent-model"),
        gateway=GatewayConfig(
            litellm_config=str(config),
            master_key=SecretStr("master-key"),
        ),
        tools={
            "browser": McpTool(
                type="mcp",
                mcp=McpToolConfig(runtime="script", command="run", port=9000),
            )
        },
        data_dir=tmp_path,
    )
    hook = MagicMock()
    hook.pynchy_mcp_server_spec.return_value = []
    plugin_manager = _FakePluginManager(hook)
    fake_manager = MagicMock()
    fake_manager.sync = AsyncMock()

    with (
        patch("pynchy.host.container_manager.gateway.get_settings", return_value=settings),
        patch(
            "pynchy.host.container_manager.security.cop_client.get_cop_gateway_config",
            return_value=("cop-model", "messages"),
        ),
        patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        patch(
            "pynchy.host.container_manager.mcp.manager.McpManager",
            return_value=fake_manager,
        ) as manager_type,
        patch("pynchy.host.container_manager.mcp.manager.set_mcp_manager") as set_manager,
    ):
        gateway = await start_gateway(plugin_manager)

    assert isinstance(gateway, LiteLLMGateway)
    manager_type.assert_called_once()
    set_manager.assert_called_once_with(fake_manager)
    fake_manager.sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_gateway_stops_and_clears_active_mcp_manager() -> None:
    mcp_manager = MagicMock()
    mcp_manager.stop_all = AsyncMock()

    with (
        patch(
            "pynchy.host.container_manager.mcp.manager.get_mcp_manager",
            return_value=mcp_manager,
        ),
        patch("pynchy.host.container_manager.mcp.manager.set_mcp_manager") as set_manager,
        patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
    ):
        await stop_gateway()

    mcp_manager.stop_all.assert_awaited_once()
    set_manager.assert_called_once_with(None)


@pytest.mark.asyncio
async def test_startup_failure_stops_gateway_without_raising() -> None:
    with patch(
        "pynchy.host.container_manager.gateway.stop_gateway", new_callable=AsyncMock
    ) as stop:
        await stop_gateway_after_startup_failure()

    stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_startup_failure_keeps_original_error_when_gateway_stop_fails() -> None:
    with patch(
        "pynchy.host.container_manager.gateway.stop_gateway",
        new_callable=AsyncMock,
        side_effect=RuntimeError("stop failed"),
    ):
        await stop_gateway_after_startup_failure()


def test_collect_yaml_environment_skips_placeholder_values(tmp_path: Path) -> None:
    config = tmp_path / "litellm.yaml"
    config.write_text("api_key: os.environ/OPENAI_API_KEY\n")

    assert (
        collect_litellm_yaml_environment(
            config,
            {"OPENAI_API_KEY": "YOUR_OPENAI_API_KEY"},  # pragma: allowlist secret
        )
        == []
    )
