"""Tests for the LLM gateway — LiteLLM and Builtin modes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings
from pydantic import SecretStr

from pynchy.config.api import AgentConfig, GatewayConfig, ProfileConfig, WorkspaceConfig
from pynchy.host.container_manager.gateway import (
    BuiltinGateway,
    LiteLLMGateway,
    get_gateway,
    start_gateway,
    stop_gateway,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# LiteLLMGateway — unit tests (Docker calls mocked)
# ---------------------------------------------------------------------------

_GATEWAY_MOD = "pynchy.host.container_manager.gateway"
_LITELLM_MOD = "pynchy.host.container_manager.gateway_litellm"
_DOCKER_MOD = "pynchy.host.container_manager.docker"
ALL_INTERFACE_BIND_HOST = "0.0.0.0"  # noqa: S104 - test data for intentional container-reachable gateway binds.

_LITELLM_KWARGS = {
    "port": 4000,
    "container_host": "host.docker.internal",
    "image": "ghcr.io/berriai/litellm:main-latest",
    "postgres_image": "postgres:17-alpine",
    "master_key": "test-master-key",
}


class TestGatewayModeSelection:
    @pytest.fixture(autouse=True)
    async def _cleanup(self):
        yield
        gateway = get_gateway()
        if gateway is not None:
            with patch.object(gateway, "stop", new_callable=AsyncMock):
                await stop_gateway()

    def test_litellm_default_image_is_pinned_to_deterministic_digest(self):
        assert GatewayConfig().litellm_image == (
            "ghcr.io/berriai/litellm@"
            "sha256:9c1f1889774a973ce650f712ace6753a9b6dd1182d25d837b858dbcac6ea3056"
        )

    @pytest.mark.asyncio
    async def test_litellm_mode_when_config_set(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: []\n")

        mock_settings = make_settings(
            agent=AgentConfig(default_core="codex", model="gpt-5.5"),
            gateway=GatewayConfig(
                litellm_config=str(cfg),
                port=4000,
                container_host="host.docker.internal",
                litellm_image="ghcr.io/berriai/litellm:main-latest",
                postgres_image="postgres:17-alpine",
                master_key=SecretStr("test-key"),
            ),
            data_dir=tmp_path,
            mcp_servers={},  # No MCP servers → skip McpManager
        )

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()
            assert isinstance(gw, LiteLLMGateway)
            assert gw.required_models == ("gpt-5.5",)

    @pytest.mark.asyncio
    async def test_litellm_mode_requires_effective_responses_cop_model(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: []\n")

        mock_settings = make_settings(
            agent=AgentConfig(default_core="codex", model="agent-model"),
            gateway=GatewayConfig(
                litellm_config=str(cfg),
                port=4000,
                container_host="host.docker.internal",
                litellm_image="ghcr.io/berriai/litellm:main-latest",
                postgres_image="postgres:17-alpine",
                master_key=SecretStr("test-key"),
            ),
            data_dir=tmp_path,
            mcp_servers={},
        )

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch(
                "pynchy.host.container_manager.security.cop_client.get_cop_gateway_config",
                return_value=("cop-responses-model", "responses"),
            ),
            patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        ):
            gateway = await start_gateway()

        assert isinstance(gateway, LiteLLMGateway)
        assert gateway.required_models == ("agent-model",)
        assert gateway.required_response_models == ("agent-model", "cop-responses-model")

    @pytest.mark.asyncio
    async def test_litellm_mode_requires_effective_workspace_models(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: []\n")

        mock_settings = make_settings(
            agent=AgentConfig(default_core="claude-cli", model="global-model"),
            profiles={"base": ProfileConfig(model="profile-model")},
            workspaces={
                "profile": WorkspaceConfig(profiles=["base"]),
                "direct": WorkspaceConfig(
                    profiles=["base"],
                    model="workspace-model",
                ),
                "duplicate": WorkspaceConfig(model="workspace-model"),
            },
            gateway=GatewayConfig(
                litellm_config=str(cfg),
                port=4000,
                container_host="host.docker.internal",
                litellm_image="ghcr.io/berriai/litellm:main-latest",
                postgres_image="postgres:17-alpine",
                master_key=SecretStr("test-key"),
            ),
            data_dir=tmp_path,
            mcp_servers={},
        )

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch(
                "pynchy.host.container_manager.security.cop_client.get_cop_gateway_config",
                return_value=("ignored-cop-model", "messages"),
            ),
            patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        ):
            gateway = await start_gateway()

        assert isinstance(gateway, LiteLLMGateway)
        assert gateway.required_models == ("global-model", "profile-model", "workspace-model")
        assert gateway.required_response_models == ()

    @pytest.mark.asyncio
    async def test_default_container_host_resolves_for_apple_runtime(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: []\n")

        mock_settings = make_settings(
            gateway=GatewayConfig(
                litellm_config=str(cfg),
                port=4000,
                container_host="host.docker.internal",
                litellm_image="ghcr.io/berriai/litellm:main-latest",
                postgres_image="postgres:17-alpine",
                master_key=SecretStr("test-key"),
            ),
            data_dir=tmp_path,
            mcp_servers={},
        )
        runtime = MagicMock()
        runtime.name = "apple"

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch(f"{_GATEWAY_MOD}._apple_container_runtime", True),
            patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()

        assert gw.base_url == "http://192.168.64.1:4000"

    @pytest.mark.asyncio
    async def test_custom_container_host_is_respected_for_apple_runtime(self, tmp_path: Path):
        cfg = tmp_path / "litellm_config.yaml"
        cfg.write_text("model_list: []\n")

        mock_settings = make_settings(
            gateway=GatewayConfig(
                litellm_config=str(cfg),
                port=4000,
                container_host="pynchy-host.local",
                litellm_image="ghcr.io/berriai/litellm:main-latest",
                postgres_image="postgres:17-alpine",
                master_key=SecretStr("test-key"),
            ),
            data_dir=tmp_path,
            mcp_servers={},
        )
        runtime = MagicMock()
        runtime.name = "apple"

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch(f"{_GATEWAY_MOD}._apple_container_runtime", True),
            patch.object(LiteLLMGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()

        assert gw.base_url == "http://pynchy-host.local:4000"

    @pytest.mark.asyncio
    async def test_builtin_mode_when_no_config(self, tmp_path: Path):
        mock_settings = make_settings(
            gateway=GatewayConfig(
                litellm_config=None,
                port=4010,
                host=ALL_INTERFACE_BIND_HOST,
                container_host="host.docker.internal",
            )
        )

        with (
            patch(f"{_GATEWAY_MOD}.get_settings", return_value=mock_settings),
            patch.object(BuiltinGateway, "start", new_callable=AsyncMock),
        ):
            gw = await start_gateway()
            assert isinstance(gw, BuiltinGateway)
