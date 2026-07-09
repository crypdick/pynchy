"""Tests for MCP proxy integration with McpManager."""

from __future__ import annotations

import asyncio
import inspect
import os
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.config.mcp import McpServerConfig
from pynchy.host.container_manager import orchestrator
from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp import litellm
from pynchy.host.container_manager.mcp.manager import McpManager
from pynchy.host.container_manager.mcp.proxy import McpProxy
from pynchy.host.container_manager.mcp.resolution import McpInstance, build_trust_map


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
        port=port,
    )


class _LiteLLMSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return None


def _make_gateway(tmp_path) -> LiteLLMGateway:
    config_path = tmp_path / "litellm_config.yaml"
    config_path.write_text("model_list: []\n")
    return LiteLLMGateway(
        config_path=str(config_path),
        port=4000,
        container_host="host.docker.internal",
        image="litellm:test",
        postgres_image="postgres:test",
        data_dir=tmp_path,
        master_key="sk-test",
    )


class TestMcpManagerHasProxy:
    """McpManager should own an McpProxy instance."""

    def test_init_creates_proxy(self, tmp_path):
        """McpManager.__init__ should create an McpProxy instance."""
        settings = make_settings(data_dir=tmp_path)
        gateway = MagicMock(spec=LiteLLMGateway)

        mgr = McpManager(settings, gateway)
        assert isinstance(mgr._proxy, McpProxy)
        assert mgr._proxy_port == 0


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

        with (
            patch(
                "pynchy.host.container_manager.mcp.litellm.aiohttp.ClientSession",
                return_value=_LiteLLMSession(),
            ),
            patch("pynchy.host.container_manager.mcp.litellm.api_request", api_request),
        ):
            await litellm.sync_mcp_endpoints(gateway, {"gdrive": _make_instance("gdrive")})


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

        with (
            patch(
                "pynchy.host.container_manager.mcp.litellm.aiohttp.ClientSession",
                return_value=_LiteLLMSession(),
            ),
            patch("pynchy.host.container_manager.mcp.litellm.api_request", api_request),
        ):
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
            patch(
                "pynchy.host.container_manager.mcp.litellm.aiohttp.ClientSession",
                return_value=_LiteLLMSession(),
            ),
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


class TestGetDirectServerConfigsProxy:
    """get_direct_server_configs should route through the proxy."""

    def test_includes_proxy_url(self):
        """Configs should contain the proxy URL pattern with group/ts/iid."""
        mgr = McpManager.__new__(McpManager)
        mgr._proxy = McpProxy()
        mgr._proxy._port = 8080
        mgr._workspace_instances = {"test-ws": ["browser_abc"]}
        mgr._instances = {
            "browser_abc": MagicMock(
                server_config=MagicMock(transport="streamable_http"),
            ),
        }

        with patch("pynchy.host.container_manager.mcp.manager.get_settings") as mock_settings:
            mock_settings.return_value.gateway.container_host = "host.docker.internal"
            configs = mgr.get_direct_server_configs("test-ws", invocation_ts=42.0)

        assert len(configs) == 1
        assert configs[0]["name"] == "browser_abc"
        assert "/mcp/test-ws/42.0/browser_abc" in configs[0]["url"]
        assert "8080" in configs[0]["url"]
        assert configs[0]["transport"] == "streamable_http"

    def test_default_container_host_resolves_for_apple_runtime(self):
        """Apple Container needs the host gateway IP, not Docker's DNS name."""
        mgr = McpManager.__new__(McpManager)
        mgr._proxy = McpProxy()
        mgr._proxy._port = 8080
        mgr._workspace_instances = {"test-ws": ["browser_abc"]}
        mgr._instances = {
            "browser_abc": MagicMock(
                server_config=MagicMock(transport="streamable_http"),
            ),
        }
        runtime = MagicMock()
        runtime.name = "apple"

        with (
            patch("pynchy.host.container_manager.mcp.manager.get_settings") as mock_settings,
            patch("pynchy.plugins.runtimes.detection.get_runtime", return_value=runtime),
        ):
            mock_settings.return_value.gateway.container_host = "host.docker.internal"
            configs = mgr.get_direct_server_configs("test-ws", invocation_ts=42.0)

        assert configs[0]["url"].startswith("http://192.168.64.1:8080/")

    def test_empty_when_no_proxy(self):
        """Should return empty list when proxy not started (port=0)."""
        mgr = McpManager.__new__(McpManager)
        mgr._proxy = McpProxy()  # port=0 (not started)
        mgr._workspace_instances = {"test-ws": ["browser"]}
        mgr._instances = {"browser": MagicMock()}

        configs = mgr.get_direct_server_configs("test-ws")
        assert configs == []

    def test_empty_when_no_instances(self):
        """Should return empty list for unknown workspace."""
        mgr = McpManager.__new__(McpManager)
        mgr._proxy = McpProxy()
        mgr._proxy._port = 8080
        mgr._workspace_instances = {}

        configs = mgr.get_direct_server_configs("unknown-ws")
        assert configs == []

    def test_skips_missing_instances(self):
        """Should skip instance IDs that don't exist in _instances dict."""
        mgr = McpManager.__new__(McpManager)
        mgr._proxy = McpProxy()
        mgr._proxy._port = 8080
        mgr._workspace_instances = {"test-ws": ["exists", "missing"]}
        mgr._instances = {
            "exists": MagicMock(
                server_config=MagicMock(transport="sse"),
            ),
        }

        with patch("pynchy.host.container_manager.mcp.manager.get_settings") as mock_settings:
            mock_settings.return_value.gateway.container_host = "host.docker.internal"
            configs = mgr.get_direct_server_configs("test-ws", invocation_ts=1.0)

        assert len(configs) == 1
        assert configs[0]["name"] == "exists"

    def test_accepts_invocation_ts_parameter(self):
        """get_direct_server_configs should accept invocation_ts parameter."""
        mgr = McpManager.__new__(McpManager)
        mgr._proxy = McpProxy()
        mgr._proxy._port = 9090
        mgr._workspace_instances = {"ws": ["svc"]}
        mgr._instances = {
            "svc": MagicMock(
                server_config=MagicMock(transport="http"),
            ),
        }

        with patch("pynchy.host.container_manager.mcp.manager.get_settings") as mock_settings:
            mock_settings.return_value.gateway.container_host = "localhost"
            configs = mgr.get_direct_server_configs("ws", invocation_ts=1234567890.123)

        assert len(configs) == 1
        assert "1234567890.123" in configs[0]["url"]


class TestStopAllStopsProxy:
    """stop_all should stop the proxy."""

    @pytest.mark.asyncio
    async def test_stop_all_calls_proxy_stop(self):
        """stop_all() should call self._proxy.stop()."""
        mgr = McpManager.__new__(McpManager)
        mgr._proxy = McpProxy()
        mgr._instances = {}
        mgr._idle_task = None
        mgr._warm_task = None

        # Spy on the proxy stop method
        original_stop = mgr._proxy.stop
        stop_called = False

        async def track_stop():
            nonlocal stop_called
            stop_called = True
            await original_stop()

        mgr._proxy.stop = track_stop

        await mgr.stop_all()
        assert stop_called


class TestOrchestratorPassesInvocationTs:
    """orchestrator.py should pass invocation_ts to get_direct_server_configs."""

    def test_spawn_container_passes_invocation_ts_to_get_direct_server_configs(self):
        """Verify _spawn_container passes invocation_ts when calling get_direct_server_configs.

        This is a structural test -- we verify the specific call pattern
        ``get_direct_server_configs(..., invocation_ts=...)`` exists in the source.
        """
        source = inspect.getsource(orchestrator._spawn_container)
        assert "get_direct_server_configs" in source
        # The invocation_ts kwarg must appear in the get_direct_server_configs call
        assert (
            "invocation_ts=input_data.invocation_ts" in source
            or "invocation_ts=" in source.split("get_direct_server_configs")[1]
        )
