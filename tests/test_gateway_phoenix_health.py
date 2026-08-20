"""Public LiteLLM startup behavior for the Phoenix dependency."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.container_manager.gateway_litellm import (
    LiteLLMGateway,
    LiteLLMGatewayCredentials,
)

if TYPE_CHECKING:
    from pathlib import Path


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


def _gateway(tmp_path: Path) -> LiteLLMGateway:
    config = tmp_path / "litellm.yaml"
    config.write_text("litellm_settings:\n  callbacks: arize_phoenix\n")
    (tmp_path / ".env").write_text(
        "PHOENIX_COLLECTOR_HTTP_ENDPOINT=https://phoenix.example.test/v1/traces\n"
    )
    return LiteLLMGateway(
        config_path=str(config),
        port=4000,
        container_host="host.docker.internal",
        image="litellm:test",
        postgres_image="postgres:test",
        data_dir=tmp_path,
        master_key="master-key",
        ui_credentials=LiteLLMGatewayCredentials(),
    )


@pytest.mark.asyncio
async def test_start_rejects_unhealthy_phoenix_dependency(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    session = MagicMock()
    session.get.return_value = _AsyncContext(MagicMock(status=503))

    with (
        patch("pynchy.host.container_manager.gateway_litellm.docker_available", return_value=True),
        patch("aiohttp.ClientSession", return_value=_AsyncContext(session)),
        pytest.raises(RuntimeError, match="Phoenix is required but not reachable"),
    ):
        await gateway.start()

    session.get.assert_called_once_with("https://phoenix.example.test/healthz")


@pytest.mark.asyncio
async def test_start_proceeds_when_phoenix_is_healthy(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    (tmp_path / ".env").write_text(
        "PHOENIX_COLLECTOR_HTTP_ENDPOINT=https://phoenix.example.test/v1/traces\n"
        "PHOENIX_API_KEY=phoenix-test-key\n"  # pragma: allowlist secret
    )
    session = MagicMock()
    session.get.return_value = _AsyncContext(MagicMock(status=204))

    with (
        patch("pynchy.host.container_manager.gateway_litellm.docker_available", return_value=True),
        patch("aiohttp.ClientSession", return_value=_AsyncContext(session)),
        patch(
            "pynchy.host.container_manager.gateway_litellm.ensure_network",
            new_callable=AsyncMock,
        ),
        patch.object(gateway, "_start_postgres", new_callable=AsyncMock),
        patch(
            "pynchy.host.container_manager.gateway_litellm.ensure_image",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.container_manager.gateway_litellm.remove_container",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.container_manager.gateway_litellm.run_docker", new_callable=AsyncMock
        ) as run_docker,
        patch("pynchy.host.container_manager.gateway_litellm.wait_healthy", new_callable=AsyncMock),
    ):
        await gateway.start()

    session.get.assert_called_once_with("https://phoenix.example.test/healthz")
    forwarded_api_key = run_docker.await_args.kwargs["environment"][
        "PHOENIX_API_KEY"
    ]  # pragma: allowlist secret
    assert forwarded_api_key == "phoenix-test-key"  # pragma: allowlist secret
