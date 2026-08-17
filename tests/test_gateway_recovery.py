"""Recovery contracts for the service-owned LiteLLM gateway."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

import pynchy.host.container_manager.gateway as gateway_module
from pynchy.host.container_manager.gateway import LiteLLMGateway

if TYPE_CHECKING:
    from pathlib import Path

_LITELLM_MOD = "pynchy.host.container_manager.gateway_litellm"
_LITELLM_KWARGS = {
    "port": 4000,
    "container_host": "host.docker.internal",
    "image": "ghcr.io/berriai/litellm:main-latest",
    "postgres_image": "postgres:17-alpine",
    "master_key": "test-master-key",
}


def _gateway(tmp_path: Path) -> LiteLLMGateway:
    return LiteLLMGateway(
        config_path=str(tmp_path / "config.yaml"),
        data_dir=tmp_path,
        **_LITELLM_KWARGS,
    )


@pytest.mark.asyncio
async def test_recovers_lost_gateway_and_resyncs_mcp(monkeypatch, tmp_path: Path):
    gateway = _gateway(tmp_path)
    mcp_manager = MagicMock()
    mcp_manager.sync = AsyncMock()
    monkeypatch.setattr(gateway_module, "get_gateway", lambda: gateway)
    monkeypatch.setattr(gateway, "is_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(gateway, "start", AsyncMock())
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.manager.get_mcp_manager",
        lambda: mcp_manager,
    )

    assert await gateway_module.recover_gateway_if_unhealthy() is True
    gateway.start.assert_awaited_once_with()
    mcp_manager.sync.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_leaves_healthy_gateway_untouched(monkeypatch, tmp_path: Path):
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(gateway_module, "get_gateway", lambda: gateway)
    monkeypatch.setattr(gateway, "is_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(gateway, "start", AsyncMock())

    assert await gateway_module.recover_gateway_if_unhealthy() is False
    gateway.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_non_litellm_gateway(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_gateway", lambda: None)

    assert await gateway_module.recover_gateway_if_unhealthy() is False


@pytest.mark.asyncio
async def test_recovery_without_mcp_manager(monkeypatch, tmp_path: Path):
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(gateway_module, "get_gateway", lambda: gateway)
    monkeypatch.setattr(gateway, "is_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(gateway, "start", AsyncMock())
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.manager.get_mcp_manager",
        lambda: None,
    )

    assert await gateway_module.recover_gateway_if_unhealthy() is True
    gateway.start.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("sidecars", [(False, True), (True, False)])
async def test_reports_unready_when_sidecar_is_missing(tmp_path: Path, sidecars):
    gateway = _gateway(tmp_path)
    with patch(f"{_LITELLM_MOD}.is_container_running", AsyncMock(side_effect=sidecars)):
        assert await gateway.is_ready() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), [(200, True), (503, False)])
async def test_checks_proxy_readiness(tmp_path: Path, status: int, expected: bool):
    async def readiness(_request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.Response(status=status)

    app = web.Application()
    app.router.add_get("/health/readiness", readiness)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        gateway = _gateway(tmp_path)
        gateway.port = site._server.sockets[0].getsockname()[1]
        with patch(f"{_LITELLM_MOD}.is_container_running", AsyncMock(return_value=True)):
            assert await gateway.is_ready() is expected
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_reports_unready_when_proxy_cannot_connect(tmp_path: Path):
    gateway = _gateway(tmp_path)
    gateway.port = 0
    with patch(f"{_LITELLM_MOD}.is_container_running", AsyncMock(return_value=True)):
        assert await gateway.is_ready() is False


@pytest.mark.asyncio
async def test_unmanaged_gateway_skips_container_lifecycle(tmp_path: Path):
    gateway = LiteLLMGateway(
        config_path=str(tmp_path / "config.yaml"),
        data_dir=tmp_path,
        managed=False,
        **_LITELLM_KWARGS,
    )
    gateway.port = 0

    assert await gateway.is_ready() is False
    await gateway.stop()


@pytest.mark.asyncio
async def test_supervisor_rechecks_after_a_healthy_probe(monkeypatch):
    sleep = AsyncMock(side_effect=(None, asyncio.CancelledError))
    recover = AsyncMock(return_value=False)
    monkeypatch.setattr(gateway_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(gateway_module, "recover_gateway_if_unhealthy", recover)

    with pytest.raises(asyncio.CancelledError):
        await gateway_module.supervise_gateway()

    recover.assert_awaited_once_with()
    assert [call.args[0] for call in sleep.await_args_list] == [5, 5]


@pytest.mark.asyncio
async def test_supervisor_backs_off_after_recovery_failure(monkeypatch):
    sleep = AsyncMock(side_effect=(None, None, asyncio.CancelledError))
    recover = AsyncMock(side_effect=RuntimeError("Docker unavailable"))
    monkeypatch.setattr(gateway_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(gateway_module, "recover_gateway_if_unhealthy", recover)

    with pytest.raises(asyncio.CancelledError):
        await gateway_module.supervise_gateway()

    recover.assert_awaited_once_with()
    assert [call.args[0] for call in sleep.await_args_list] == [5, 1, 5]
