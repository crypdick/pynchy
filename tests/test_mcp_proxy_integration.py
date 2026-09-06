"""Tests for MCP proxy integration with McpManager."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import ClientSession, web

from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp import litellm
from pynchy.host.container_manager.mcp.manager import (
    DirectMcpServerConfigRequest,
    build_direct_server_configs,
)
from pynchy.host.container_manager.mcp.resolution import McpInstance, WorkspaceTeam, build_trust_map
from pynchy.plugins.api import McpServerConfig


def _make_instance(
    server_name: str,
    *,
    instance_id: str | None = None,
    transport: str = "sse",
    auth_value_env: str | None = None,
    port: int = 8931,
) -> McpInstance:
    """Minimal real McpInstance — build_trust_map only reads .server_name."""
    return McpInstance(
        server_name=server_name,
        server_config=McpServerConfig(
            type="script",
            command="noop",
            port=port,
            transport=transport,
            auth_value_env=auth_value_env,
        ),
        kwargs={},
        instance_id=instance_id or server_name,
        container_name=server_name,
        project_root=Path("/project"),
        port=port,
    )


def _make_gateway(tmp_path, *, port: int = 4000) -> LiteLLMGateway:
    config_path = tmp_path / "litellm_config.yaml"
    config_path.write_text("model_list: []\n")
    return LiteLLMGateway(
        config_path=str(config_path),
        port=port,
        container_host="host.docker.internal",
        image="litellm:test",
        postgres_image="postgres:test",
        data_dir=tmp_path,
        master_key="sk-test",
    )


async def _start_http_server(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert server is not None
    return runner, server.sockets[0].getsockname()[1]


def _build_direct_configs(
    *,
    group_folder: str = "test-ws",
    instance_ids: tuple[str, ...] = (),
    instances: dict[str, McpInstance] | None = None,
    proxy_port: int = 8080,
    container_host: str = "host.docker.internal",
    invocation_ts: float = 0.0,
) -> list[dict[str, str]]:
    return build_direct_server_configs(
        DirectMcpServerConfigRequest(
            group_folder=group_folder,
            instance_ids=instance_ids,
            instances=instances or {},
            proxy_port=proxy_port,
            container_host=container_host,
            invocation_ts=invocation_ts,
        )
    )


class TestBuildTrustMap:
    """_build_trust_map should produce safe defaults for every instance."""

    def test_defaults_to_not_public(self):
        """Default trust map should mark all instances as not public_source."""
        instances = {
            "browser_abc": _make_instance("browser"),
            "notebook_def": _make_instance("notebook"),
        }

        trust_map = build_trust_map(instances, {})
        assert trust_map["browser_abc"]["public_source"] is False
        assert trust_map["notebook_def"]["public_source"] is False

    def test_keys_match_instances(self):
        """Trust map keys should exactly match instance IDs."""
        instances = {
            "a": _make_instance("a"),
            "b": _make_instance("b"),
            "c": _make_instance("c"),
        }

        trust_map = build_trust_map(instances, {})
        assert set(trust_map.keys()) == {"a", "b", "c"}


class TestLiteLLMSyncRuntimeTypes:
    """Runtime type hints used by beartype should be importable."""

    @pytest.mark.asyncio
    async def test_sync_mcp_endpoints_accepts_real_gateway(self, tmp_path):
        """sync_mcp_endpoints should not crash resolving LiteLLMGateway."""
        gateway = _make_gateway(tmp_path)

        def api_request(*_args, **_kwargs):
            return asyncio.sleep(0, result=[])

        with patch("pynchy.host.container_manager.mcp.litellm.api_request", api_request):
            await litellm.sync_mcp_endpoints(gateway, {"gdrive": _make_instance("gdrive")})


class TestLiteLLMApiRequest:
    @pytest.mark.asyncio
    async def test_sends_authenticated_json_and_returns_the_management_response(self, tmp_path):
        received: dict[str, object] = {}

        async def create_team(request: web.Request) -> web.Response:
            received["authorization"] = request.headers["Authorization"]
            received["body"] = await request.json()
            return web.json_response({"team_id": "team-1"}, status=201)

        app = web.Application()
        app.router.add_post("/team/new", create_team)
        runner, port = await _start_http_server(app)
        gateway = _make_gateway(tmp_path, port=port)

        try:
            async with ClientSession() as session:
                response = await litellm.api_request(
                    session,
                    gateway,
                    "POST",
                    "/team/new",
                    json_data={"team_alias": "ops"},
                )
        finally:
            await runner.cleanup()

        assert response == {"team_id": "team-1"}
        assert received == {
            "authorization": "Bearer sk-test",  # pragma: allowlist secret
            "body": {"team_alias": "ops"},
        }

    @pytest.mark.asyncio
    async def test_treats_empty_success_as_success_and_rejected_request_as_failure(self, tmp_path):
        async def empty_response(_request: web.Request) -> web.Response:
            await asyncio.sleep(0)
            return web.Response(status=200)

        async def accepted_response(_request: web.Request) -> web.Response:
            await asyncio.sleep(0)
            return web.Response(status=202)

        async def rejected_response(_request: web.Request) -> web.Response:
            await asyncio.sleep(0)
            return web.Response(status=403, text="denied")

        app = web.Application()
        app.router.add_get("/empty", empty_response)
        app.router.add_delete("/accepted", accepted_response)
        app.router.add_get("/rejected", rejected_response)
        runner, port = await _start_http_server(app)
        gateway = _make_gateway(tmp_path, port=port)

        try:
            async with ClientSession() as session:
                empty = await litellm.api_request(session, gateway, "GET", "/empty")
                accepted = await litellm.api_request(session, gateway, "DELETE", "/accepted")
                rejected = await litellm.api_request(session, gateway, "GET", "/rejected")
        finally:
            await runner.cleanup()

        assert empty is True
        assert accepted is True
        assert rejected is None


class TestLiteLLMSyncEndpoints:
    @pytest.mark.asyncio
    async def test_sync_mcp_endpoints_deduplicates_and_deletes_stale_entries(self, tmp_path):
        gateway = _make_gateway(tmp_path)
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def api_request(_session, _gateway, method, path, *, json_data=None, **_kwargs):
            calls.append((method, path, json_data))
            if method == "GET" and path == "/v1/mcp/server":
                return asyncio.sleep(
                    0,
                    result=[
                        {
                            "server_name": "gdrive_team",
                            "url": "http://localhost:8931/mcp",
                            "server_id": "keep",
                        },
                        {
                            "server_name": "gdrive_team",
                            "url": "http://stale-gdrive",
                            "server_id": "drop-me",
                        },
                        {
                            "server_name": "stale_service",
                            "url": "http://stale-service",
                            "server_id": "remove-me",
                        },
                    ],
                )
            return asyncio.sleep(0, result=True)

        instances = {
            "gdrive.team": _make_instance("gdrive", instance_id="gdrive.team"),
        }

        with patch("pynchy.host.container_manager.mcp.litellm.api_request", api_request):
            await litellm.sync_mcp_endpoints(gateway, instances)

        assert ("DELETE", "/v1/mcp/server/drop-me", None) in calls
        assert ("DELETE", "/v1/mcp/server/remove-me", None) in calls
        assert ("POST", "/v1/mcp/server", None) not in calls

    @pytest.mark.asyncio
    async def test_sync_mcp_endpoints_registers_missing_instance(self, tmp_path):
        gateway = _make_gateway(tmp_path)
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def api_request(_session, _gateway, method, path, *, json_data=None, **_kwargs):
            calls.append((method, path, json_data))
            if method == "GET" and path == "/v1/mcp/server":
                return asyncio.sleep(0, result=[])
            return asyncio.sleep(0, result=True)

        instance = _make_instance(
            "notebook",
            instance_id="notebook-service",
            transport="streamable_http",
            auth_value_env="NOTEBOOK_TOKEN",
            port=9123,
        )

        with (
            patch.dict(os.environ, {"NOTEBOOK_TOKEN": "secret-token"}),
            patch("pynchy.host.container_manager.mcp.litellm.api_request", api_request),
        ):
            await litellm.sync_mcp_endpoints(gateway, {instance.instance_id: instance})

        assert ("GET", "/v1/mcp/server", None) in calls
        assert (
            "POST",
            "/v1/mcp/server",
            {
                "server_name": "notebook_service",
                "url": "http://localhost:9123/mcp",
                "transport": "http",
                "allow_all_keys": True,
                "auth_value": "secret-token",
            },
        ) in calls

    @pytest.mark.asyncio
    async def test_sync_mcp_endpoints_rechecks_an_empty_startup_inventory(self, tmp_path):
        gateway = _make_gateway(tmp_path)
        instance = _make_instance("linear", instance_id="linear_team", port=8486)
        calls: list[tuple[str, str]] = []
        list_calls = 0

        async def api_request(_session, _gateway, method, path, *, json_data=None, **_kwargs):
            nonlocal list_calls
            await asyncio.sleep(0)
            calls.append((method, path))
            if method != "GET":
                return True
            list_calls += 1
            if list_calls == 1:
                return []
            return [
                {
                    "server_name": "linear_team",
                    "url": "http://localhost:8485",
                    "server_id": "stale-control-port",
                },
                {
                    "server_name": "linear_team",
                    "url": instance.endpoint_url,
                    "server_id": "keep",
                },
            ]

        with patch("pynchy.host.container_manager.mcp.litellm.api_request", api_request):
            await litellm.sync_mcp_endpoints(gateway, {instance.instance_id: instance})

        assert list_calls == 2
        assert ("DELETE", "/v1/mcp/server/stale-control-port") in calls

    @pytest.mark.asyncio
    async def test_sync_mcp_endpoints_omits_an_unset_auth_value(self, tmp_path, monkeypatch):
        gateway = _make_gateway(tmp_path)
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def api_request(_session, _gateway, method, path, *, json_data=None, **_kwargs):
            calls.append((method, path, json_data))
            if method == "GET" and path == "/v1/mcp/server":
                return asyncio.sleep(0, result=[])
            return asyncio.sleep(0, result=True)

        instance = _make_instance(
            "notebook",
            instance_id="notebook-service",
            auth_value_env="NOTEBOOK_TOKEN",
        )
        monkeypatch.delenv("NOTEBOOK_TOKEN", raising=False)

        with patch("pynchy.host.container_manager.mcp.litellm.api_request", api_request):
            await litellm.sync_mcp_endpoints(gateway, {instance.instance_id: instance})

        payload = next(json_data for method, path, json_data in calls if method == "POST")
        assert payload is not None
        assert "auth_value" not in payload


class TestLiteLLMWorkspaceTeams:
    @pytest.mark.asyncio
    async def test_sync_teams_creates_new_updates_current_and_deletes_stale(self, tmp_path):
        gateway = _make_gateway(tmp_path)
        calls: list[tuple[str, str, dict[str, object] | None]] = []
        workspace_teams = {
            "current": WorkspaceTeam(team_id="team-current", virtual_key="key-current"),
            "stale": WorkspaceTeam(team_id="team-stale", virtual_key="key-stale"),
        }

        def api_request(_session, _gateway, method, path, *, json_data=None, **_kwargs):
            calls.append((method, path, json_data))
            if path == "/team/new":
                return asyncio.sleep(0, result={"team_id": "team-new"})
            if path == "/key/generate":
                return asyncio.sleep(0, result={"key": "key-new"})
            return asyncio.sleep(0, result=True)

        with patch("pynchy.host.container_manager.mcp.litellm.api_request", api_request):
            await litellm.sync_teams(
                gateway,
                {"new": ["gdrive"], "current": ["notebook"]},
                workspace_teams,
            )

        assert workspace_teams == {
            "new": WorkspaceTeam(team_id="team-new", virtual_key="key-new"),
            "current": WorkspaceTeam(team_id="team-current", virtual_key="key-current"),
        }
        assert (
            "POST",
            "/team/new",
            {"team_alias": "pynchy-mcp-new", "metadata": {"pynchy_workspace": "new"}},
        ) in calls
        assert (
            "POST",
            "/key/generate",
            {"team_id": "team-new", "allowed_mcp_servers": ["gdrive"]},
        ) in calls
        assert (
            "POST",
            "/team/update",
            {"team_id": "team-current", "metadata": {"allowed_mcp_servers": ["notebook"]}},
        ) in calls
        assert ("POST", "/team/delete", {"team_ids": ["team-stale"]}) in calls

    def test_team_cache_round_trips_and_discards_malformed_data(self, tmp_path):
        cache_path = tmp_path / "mcp" / "teams.json"
        teams = {"ops": WorkspaceTeam(team_id="team-ops", virtual_key="key-ops")}

        litellm.save_teams_cache(cache_path, teams)

        assert litellm.load_teams_cache(cache_path) == teams
        cache_path.write_text('{"ops": {"team_id": "team-ops"}}', encoding="utf-8")
        assert litellm.load_teams_cache(cache_path) == {}


class TestBuildDirectServerConfigs:
    """Direct agent MCP configs must route only ready instances through the proxy."""

    def test_exposes_logical_name_for_workspace_specific_instance(self):
        configs = _build_direct_configs(
            group_folder="pynchy-dev",
            instance_ids=("linear_e0d492",),
            instances={
                "linear_e0d492": _make_instance(
                    "linear",
                    instance_id="linear_e0d492",
                    transport="streamable_http",
                )
            },
            invocation_ts=42.0,
        )

        assert configs == [
            {
                "name": "linear",
                "url": "http://host.docker.internal:8080/mcp/pynchy-dev/42.0/linear_e0d492",
                "transport": "streamable_http",
            }
        ]

    def test_includes_proxy_url(self):
        """Agent names stay stable while proxy URLs retain instance identity."""
        configs = _build_direct_configs(
            instance_ids=("browser_abc",),
            instances={
                "browser_abc": _make_instance(
                    "browser",
                    instance_id="browser_abc",
                    transport="streamable_http",
                )
            },
            invocation_ts=42.0,
        )

        assert len(configs) == 1
        assert configs[0]["name"] == "browser"
        assert "/mcp/test-ws/42.0/browser_abc" in configs[0]["url"]
        assert "8080" in configs[0]["url"]
        assert configs[0]["transport"] == "streamable_http"

    def test_default_container_host_resolves_for_apple_runtime(self):
        """Apple Container needs the host gateway IP, not Docker's DNS name."""
        with patch("pynchy.host.container_manager.gateway._apple_container_runtime", True):
            configs = _build_direct_configs(
                instance_ids=("browser_abc",),
                instances={"browser_abc": _make_instance("browser", transport="streamable_http")},
                invocation_ts=42.0,
            )

        assert configs[0]["url"].startswith("http://192.168.64.1:8080/")

    def test_empty_when_no_proxy(self):
        """Should return empty list when proxy not started (port=0)."""
        configs = _build_direct_configs(
            instance_ids=("browser",),
            instances={"browser": _make_instance("browser")},
            proxy_port=0,
        )
        assert configs == []

    def test_empty_when_no_instances(self):
        """Should return empty list for unknown workspace."""
        configs = _build_direct_configs(
            group_folder="unknown-ws",
        )
        assert configs == []

    def test_skips_missing_instances(self):
        """Should skip selected instance IDs absent from the resolved set."""
        configs = _build_direct_configs(
            instance_ids=("exists", "missing"),
            instances={"exists": _make_instance("exists")},
            invocation_ts=1.0,
        )

        assert len(configs) == 1
        assert configs[0]["name"] == "exists"

    def test_limits_configs_to_instances_that_started_successfully(self):
        """A failed optional MCP must not be advertised to the new agent."""
        configs = _build_direct_configs(
            instance_ids=("ready",),
            instances={
                "ready": _make_instance("ready"),
                "failed": _make_instance("failed"),
            },
            invocation_ts=1.0,
        )

        assert [config["name"] for config in configs] == ["ready"]

    def test_accepts_invocation_ts_parameter(self):
        """Invocation time scopes every proxy route to one agent session."""
        configs = _build_direct_configs(
            group_folder="ws",
            instance_ids=("svc",),
            instances={"svc": _make_instance("svc", transport="http")},
            proxy_port=9090,
            container_host="localhost",
            invocation_ts=1234567890.123,
        )

        assert len(configs) == 1
        assert "1234567890.123" in configs[0]["url"]
