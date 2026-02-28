"""Tests for MCP proxy integration with McpManager."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pynchy.host.container_manager.mcp.proxy import McpProxy
from pynchy.host.container_manager.mcp.resolution import build_trust_map


class TestMcpManagerHasProxy:
    """McpManager should own an McpProxy instance."""

    def test_init_creates_proxy(self):
        """McpManager.__init__ should create an McpProxy instance."""
        from pynchy.host.container_manager.mcp.manager import McpManager

        settings = MagicMock()
        settings.data_dir = MagicMock()
        settings.data_dir.__truediv__ = MagicMock(return_value=MagicMock())
        gateway = MagicMock()

        mgr = McpManager(settings, gateway)
        assert isinstance(mgr._proxy, McpProxy)
        assert mgr._proxy_port == 0


class TestBuildTrustMap:
    """_build_trust_map should produce safe defaults for every instance."""

    def test_defaults_to_not_public(self):
        """Default trust map should mark all instances as not public_source."""
        instances = {
            "browser_abc": MagicMock(server_name="browser"),
            "notebook_def": MagicMock(server_name="notebook"),
        }

        trust_map = build_trust_map(instances, {})
        assert trust_map["browser_abc"]["public_source"] is False
        assert trust_map["notebook_def"]["public_source"] is False

    def test_keys_match_instances(self):
        """Trust map keys should exactly match instance IDs."""
        instances = {
            "a": MagicMock(server_name="a"),
            "b": MagicMock(server_name="b"),
            "c": MagicMock(server_name="c"),
        }

        trust_map = build_trust_map(instances, {})
        assert set(trust_map.keys()) == {"a", "b", "c"}


class TestGetDirectServerConfigsProxy:
    """get_direct_server_configs should route through the proxy."""

    def test_includes_proxy_url(self):
        """Configs should contain the proxy URL pattern with group/ts/iid."""
        from pynchy.host.container_manager.mcp.manager import McpManager

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

    def test_empty_when_no_proxy(self):
        """Should return empty list when proxy not started (port=0)."""
        from pynchy.host.container_manager.mcp.manager import McpManager

        mgr = McpManager.__new__(McpManager)
        mgr._proxy = McpProxy()  # port=0 (not started)
        mgr._workspace_instances = {"test-ws": ["browser"]}
        mgr._instances = {"browser": MagicMock()}

        configs = mgr.get_direct_server_configs("test-ws")
        assert configs == []

    def test_empty_when_no_instances(self):
        """Should return empty list for unknown workspace."""
        from pynchy.host.container_manager.mcp.manager import McpManager

        mgr = McpManager.__new__(McpManager)
        mgr._proxy = McpProxy()
        mgr._proxy._port = 8080
        mgr._workspace_instances = {}

        configs = mgr.get_direct_server_configs("unknown-ws")
        assert configs == []

    def test_skips_missing_instances(self):
        """Should skip instance IDs that don't exist in _instances dict."""
        from pynchy.host.container_manager.mcp.manager import McpManager

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
        from pynchy.host.container_manager.mcp.manager import McpManager

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
        from pynchy.host.container_manager.mcp.manager import McpManager

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
        import inspect

        from pynchy.host.container_manager import orchestrator

        source = inspect.getsource(orchestrator._spawn_container)
        assert "get_direct_server_configs" in source
        # The invocation_ts kwarg must appear in the get_direct_server_configs call
        assert (
            "invocation_ts=input_data.invocation_ts" in source
            or "invocation_ts=" in source.split("get_direct_server_configs")[1]
        )
