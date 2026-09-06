"""Public MCP/LiteLLM synchronization failure contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import ClientSession, web

from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp import litellm
from pynchy.host.container_manager.mcp.resolution import McpInstance, WorkspaceTeam
from pynchy.plugins.api import McpServerConfig


def _gateway(tmp_path: Path, *, port: int = 4000) -> LiteLLMGateway:
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n")
    return LiteLLMGateway(
        config_path=str(config),
        port=port,
        container_host="host.docker.internal",
        image="litellm:test",
        postgres_image="postgres:test",
        data_dir=tmp_path,
        master_key="master-key",
    )


def _instance(name: str) -> McpInstance:
    return McpInstance(
        server_name=name,
        server_config=McpServerConfig(type="script", command="noop", port=8931),
        kwargs={},
        instance_id=name,
        container_name=name,
        project_root=Path("/project"),
        port=8931,
    )


async def _start_http_server(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert server is not None
    return runner, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_api_request_returns_none_on_connection_failure(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, port=1)

    async with aiohttp.ClientSession() as session:
        result = await litellm.api_request(
            session,
            gateway,
            "GET",
            "/v1/mcp/server",
            log_event="MCP list failed",
        )

    assert result is None


@pytest.mark.asyncio
async def test_api_request_suppresses_unlogged_connection_failure(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, port=1)

    async with aiohttp.ClientSession() as session:
        result = await litellm.api_request(session, gateway, "GET", "/v1/mcp/server")

    assert result is None


@pytest.mark.asyncio
async def test_api_request_logs_and_returns_none_for_rejected_response(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def rejected(_request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.Response(status=503, text="x" * 500 + "discarded-tail")

    app = web.Application()
    app.router.add_get("/rejected", rejected)
    runner, port = await _start_http_server(app)
    gateway = _gateway(tmp_path, port=port)

    try:
        async with ClientSession() as session:
            result = await litellm.api_request(
                session,
                gateway,
                "GET",
                "/rejected",
                log_event="MCP request rejected",
                workspace="ops",
                response="retryable",
            )
    finally:
        await runner.cleanup()

    assert result is None
    assert "MCP request rejected" in caplog.text
    assert "workspace=ops" in caplog.text
    assert "response=retryable" in caplog.text
    assert "x" * 500 in caplog.text
    assert "discarded-tail" not in caplog.text


@pytest.mark.asyncio
async def test_endpoint_sync_survives_litellm_list_and_register_failures(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path)

    async def failed_request(
        _session: object,
        _gateway: object,
        method: str,
        _path: str,
        **_kwargs: object,
    ) -> object:
        await asyncio.sleep(0)
        if method == "GET":
            return [None, {"server_name": 123}]
        return None

    with patch(
        "pynchy.host.container_manager.mcp.litellm.api_request",
        side_effect=failed_request,
    ):
        await litellm.sync_mcp_endpoints(gateway, {"browser": _instance("browser")})


@pytest.mark.asyncio
async def test_endpoint_sync_treats_non_list_server_response_as_empty(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    async def malformed_list(
        _session: object,
        _gateway: object,
        method: str,
        _path: str,
        **_kwargs: object,
    ) -> object:
        await asyncio.sleep(0)
        return {} if method == "GET" else None

    with patch(
        "pynchy.host.container_manager.mcp.litellm.api_request",
        side_effect=malformed_list,
    ):
        await litellm.sync_mcp_endpoints(gateway, {})


@pytest.mark.asyncio
async def test_endpoint_sync_keeps_exact_registration_and_skips_blank_stale_ids(
    tmp_path: Path,
) -> None:
    gateway = _gateway(tmp_path)
    instance = _instance("browser")
    calls: list[tuple[str, str]] = []

    async def request(
        _session: object,
        _gateway: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> object:
        await asyncio.sleep(0)
        calls.append((method, path))
        if method == "GET":
            return [
                {
                    "server_name": "browser",
                    "url": instance.endpoint_url,
                    "server_id": "keep",
                },
                {"server_name": "browser", "url": "stale", "server_id": ""},
                {"server_name": "browser", "url": "stale", "server_id": "stale"},
            ]
        return method != "DELETE"

    with patch("pynchy.host.container_manager.mcp.litellm.api_request", side_effect=request):
        await litellm.sync_mcp_endpoints(gateway, {"browser": instance})

    assert ("POST", "/v1/mcp/server") not in calls
    assert ("DELETE", "/v1/mcp/server/") not in calls
    assert ("DELETE", "/v1/mcp/server/stale") in calls


@pytest.mark.asyncio
async def test_team_is_not_cached_when_virtual_key_creation_fails(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    workspace_teams: dict[str, WorkspaceTeam] = {}

    async def partial_request(
        _session: object,
        _gateway: object,
        _method: str,
        path: str,
        **_kwargs: object,
    ) -> object:
        await asyncio.sleep(0)
        if path == "/team/new":
            return {"team_id": "team-new"}
        return None

    with patch(
        "pynchy.host.container_manager.mcp.litellm.api_request",
        side_effect=partial_request,
    ):
        await litellm.sync_teams(gateway, {"workspace": ["browser"]}, workspace_teams)

    assert workspace_teams == {}


@pytest.mark.asyncio
async def test_team_is_not_cached_when_team_creation_fails(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    workspace_teams: dict[str, WorkspaceTeam] = {}

    async def failed_creation(
        _session: object,
        _gateway: object,
        _method: str,
        _path: str,
        **_kwargs: object,
    ) -> object:
        await asyncio.sleep(0)
        return None

    with patch(
        "pynchy.host.container_manager.mcp.litellm.api_request",
        side_effect=failed_creation,
    ):
        await litellm.sync_teams(gateway, {"workspace": ["browser"]}, workspace_teams)

    assert workspace_teams == {}


def test_team_cache_load_returns_empty_for_missing_file(tmp_path: Path) -> None:
    assert litellm.load_teams_cache(tmp_path / "missing-teams.json") == {}
