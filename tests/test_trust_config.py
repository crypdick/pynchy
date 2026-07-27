"""Tests for unified trust config — plugin defaults and instance expansion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pluggy

from pynchy.host.container_manager.gateway import collect_plugin_mcp_servers
from pynchy.host.container_manager.mcp.resolution import McpInstance, build_trust_map
from pynchy.plugins.contracts import McpServerSpec
from pynchy.plugins.mcp_server import McpServerConfig
from pynchy.types import ServiceTrustConfig


class _FakePM(pluggy.PluginManager):
    """Real-class stand-in so isinstance(pm, pluggy.PluginManager) succeeds."""

    def __init__(self, hook: MagicMock) -> None:
        self.hook = hook


def _make_instance(server_name: str) -> McpInstance:
    """Minimal real McpInstance — build_trust_map only reads .server_name."""
    return McpInstance(
        server_name=server_name,
        server_config=McpServerConfig(type="script", command="noop", port=0),
        kwargs={},
        instance_id=server_name,
        container_name=server_name,
        project_root=Path("/project"),
    )


class TestPluginTrustExtraction:
    def test_extract_trust_from_plugin_spec(self):
        """Typed plugin trust defaults remain separate from server runtime config."""
        hook = MagicMock()
        hook.pynchy_mcp_server_spec.return_value = [
            (
                McpServerSpec(
                    name="browser",
                    config=McpServerConfig(
                        type="script",
                        command="npx",
                        args=["@anthropic-ai/playwright-mcp"],
                        port=9100,
                        transport="streamable_http",
                    ),
                    trust=ServiceTrustConfig(
                        public_source=True,
                        secret_data=False,
                        public_sink=False,
                        dangerous_writes=False,
                    ),
                ),
            )
        ]
        fake_pm = _FakePM(hook)

        servers, trust_defaults = collect_plugin_mcp_servers(fake_pm)
        assert "browser" in servers
        assert "browser" in trust_defaults
        assert trust_defaults["browser"].public_source is True
        assert trust_defaults["browser"].secret_data is False

    def test_spec_without_trust_has_no_default(self):
        """Specs without trust defaults should not appear in trust_defaults."""
        hook = MagicMock()
        hook.pynchy_mcp_server_spec.return_value = [
            (
                McpServerSpec(
                    name="notebook",
                    config=McpServerConfig(
                        type="script",
                        command="uv",
                        args=["run", "notebook.py"],
                        port=8888,
                        transport="streamable_http",
                    ),
                ),
            )
        ]
        fake_pm = _FakePM(hook)

        servers, trust_defaults = collect_plugin_mcp_servers(fake_pm)
        assert "notebook" in servers
        assert "notebook" not in trust_defaults

    def test_trust_is_not_part_of_runtime_config(self):
        """Trust metadata does not leak into the strict MCP runtime model."""
        hook = MagicMock()
        hook.pynchy_mcp_server_spec.return_value = [
            (
                McpServerSpec(
                    name="risky",
                    config=McpServerConfig(
                        type="script",
                        command="node",
                        args=["server.js"],
                        port=3000,
                        transport="sse",
                    ),
                    trust=ServiceTrustConfig(public_source=True),
                ),
            )
        ]
        fake_pm = _FakePM(hook)

        # Should not raise ValidationError from extra="forbid"
        servers, _trust_defaults = collect_plugin_mcp_servers(fake_pm)
        assert "risky" in servers

    def test_multiple_specs_with_mixed_trust(self):
        """Multiple specs: some with trust, some without."""
        hook = MagicMock()
        hook.pynchy_mcp_server_spec.return_value = [
            (
                McpServerSpec(
                    name="a",
                    config=McpServerConfig(
                        type="script", command="cmd_a", port=9001, transport="sse"
                    ),
                    trust=ServiceTrustConfig(public_source=True, dangerous_writes=True),
                ),
                McpServerSpec(
                    name="b",
                    config=McpServerConfig(
                        type="script", command="cmd_b", port=9002, transport="sse"
                    ),
                ),
            )
        ]
        fake_pm = _FakePM(hook)

        servers, trust_defaults = collect_plugin_mcp_servers(fake_pm)
        assert "a" in servers
        assert "b" in servers
        assert "a" in trust_defaults
        assert "b" not in trust_defaults
        assert trust_defaults["a"].dangerous_writes is True


class TestBuildTrustMapWithPluginDefaults:
    def test_uses_plugin_trust_defaults(self):
        """build_trust_map should use plugin trust defaults for matching servers."""
        instances = {
            "browser_abc": _make_instance("browser"),
        }
        plugin_trust_defaults = {
            "browser": ServiceTrustConfig(public_source=True, secret_data=False),
        }

        trust_map = build_trust_map(instances, plugin_trust_defaults)
        assert trust_map["browser_abc"]["public_source"] is True
        assert trust_map["browser_abc"]["secret_data"] is False

    def test_falls_back_to_safe_default(self):
        """Instances without plugin trust should get safe defaults."""
        instances = {
            "unknown_xyz": _make_instance("unknown"),
        }

        trust_map = build_trust_map(instances, {})
        assert trust_map["unknown_xyz"]["public_source"] is False

    def test_trust_map_includes_all_fields(self):
        """When plugin trust is present, all four trust fields should be in the map."""
        instances = {
            "email_srv": _make_instance("email"),
        }
        plugin_trust_defaults = {
            "email": ServiceTrustConfig(
                public_source=True,
                secret_data=True,
                public_sink=True,
                dangerous_writes=False,
            ),
        }

        trust_map = build_trust_map(instances, plugin_trust_defaults)
        entry = trust_map["email_srv"]
        assert entry["public_source"] is True
        assert entry["secret_data"] is True
        assert entry["public_sink"] is True
        assert entry["dangerous_writes"] is False

    def test_multiple_instances_same_server(self):
        """Multiple instances of the same server should all get the same plugin trust."""
        instances = {
            "browser_ws1": _make_instance("browser"),
            "browser_ws2": _make_instance("browser"),
        }
        plugin_trust_defaults = {
            "browser": ServiceTrustConfig(public_source=True, secret_data=False),
        }

        trust_map = build_trust_map(instances, plugin_trust_defaults)
        assert trust_map["browser_ws1"]["public_source"] is True
        assert trust_map["browser_ws2"]["public_source"] is True
