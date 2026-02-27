"""Tests for unified trust config — plugin defaults and instance expansion."""

from __future__ import annotations

from unittest.mock import MagicMock

from pynchy.types import ServiceTrustConfig


class TestPluginTrustExtraction:
    def test_extract_trust_from_plugin_spec(self):
        """Plugin specs with 'trust' should have it extracted before McpServerConfig validation."""
        from pynchy.container_runner.gateway import _collect_plugin_mcp_servers

        fake_pm = MagicMock()
        fake_pm.hook.pynchy_mcp_server_spec.return_value = [
            {
                "name": "browser",
                "command": "npx",
                "args": ["@anthropic-ai/playwright-mcp"],
                "port": 9100,
                "transport": "streamable_http",
                "trust": {
                    "public_source": True,
                    "secret_data": False,
                    "public_sink": False,
                    "dangerous_writes": False,
                },
            }
        ]

        servers, trust_defaults = _collect_plugin_mcp_servers(fake_pm)
        assert "browser" in servers
        assert "browser" in trust_defaults
        assert trust_defaults["browser"].public_source is True
        assert trust_defaults["browser"].secret_data is False

    def test_spec_without_trust_has_no_default(self):
        """Specs without a trust key should not appear in trust_defaults."""
        from pynchy.container_runner.gateway import _collect_plugin_mcp_servers

        fake_pm = MagicMock()
        fake_pm.hook.pynchy_mcp_server_spec.return_value = [
            {
                "name": "notebook",
                "command": "uv",
                "args": ["run", "notebook.py"],
                "port": 8888,
                "transport": "streamable_http",
            }
        ]

        servers, trust_defaults = _collect_plugin_mcp_servers(fake_pm)
        assert "notebook" in servers
        assert "notebook" not in trust_defaults

    def test_trust_not_passed_to_model_validate(self):
        """The trust key must be popped before McpServerConfig.model_validate (extra=forbid)."""
        from pynchy.container_runner.gateway import _collect_plugin_mcp_servers

        fake_pm = MagicMock()
        fake_pm.hook.pynchy_mcp_server_spec.return_value = [
            {
                "name": "risky",
                "command": "node",
                "args": ["server.js"],
                "port": 3000,
                "transport": "sse",
                "trust": {"public_source": True},
            }
        ]

        # Should not raise ValidationError from extra="forbid"
        servers, trust_defaults = _collect_plugin_mcp_servers(fake_pm)
        assert "risky" in servers

    def test_multiple_specs_with_mixed_trust(self):
        """Multiple specs: some with trust, some without."""
        from pynchy.container_runner.gateway import _collect_plugin_mcp_servers

        fake_pm = MagicMock()
        fake_pm.hook.pynchy_mcp_server_spec.return_value = [
            {
                "name": "a",
                "command": "cmd_a",
                "args": [],
                "port": 9001,
                "transport": "sse",
                "trust": {"public_source": True, "dangerous_writes": True},
            },
            {
                "name": "b",
                "command": "cmd_b",
                "args": [],
                "port": 9002,
                "transport": "sse",
            },
        ]

        servers, trust_defaults = _collect_plugin_mcp_servers(fake_pm)
        assert "a" in servers and "b" in servers
        assert "a" in trust_defaults
        assert "b" not in trust_defaults
        assert trust_defaults["a"].dangerous_writes is True


class TestBuildTrustMapWithPluginDefaults:
    def test_uses_plugin_trust_defaults(self):
        """_build_trust_map should use plugin trust defaults for matching servers."""
        from pynchy.container_runner.mcp_manager import McpManager

        mgr = McpManager.__new__(McpManager)
        mgr._instances = {
            "browser_abc": MagicMock(server_name="browser"),
        }
        mgr._plugin_trust_defaults = {
            "browser": ServiceTrustConfig(public_source=True, secret_data=False),
        }

        trust_map = mgr._build_trust_map()
        assert trust_map["browser_abc"]["public_source"] is True
        assert trust_map["browser_abc"]["secret_data"] is False

    def test_falls_back_to_safe_default(self):
        """Instances without plugin trust should get safe defaults."""
        from pynchy.container_runner.mcp_manager import McpManager

        mgr = McpManager.__new__(McpManager)
        mgr._instances = {
            "unknown_xyz": MagicMock(server_name="unknown"),
        }
        mgr._plugin_trust_defaults = {}

        trust_map = mgr._build_trust_map()
        assert trust_map["unknown_xyz"]["public_source"] is False

    def test_trust_map_includes_all_fields(self):
        """When plugin trust is present, all four trust fields should be in the map."""
        from pynchy.container_runner.mcp_manager import McpManager

        mgr = McpManager.__new__(McpManager)
        mgr._instances = {
            "email_srv": MagicMock(server_name="email"),
        }
        mgr._plugin_trust_defaults = {
            "email": ServiceTrustConfig(
                public_source=True,
                secret_data=True,
                public_sink=True,
                dangerous_writes=False,
            ),
        }

        trust_map = mgr._build_trust_map()
        entry = trust_map["email_srv"]
        assert entry["public_source"] is True
        assert entry["secret_data"] is True
        assert entry["public_sink"] is True
        assert entry["dangerous_writes"] is False

    def test_multiple_instances_same_server(self):
        """Multiple instances of the same server should all get the same plugin trust."""
        from pynchy.container_runner.mcp_manager import McpManager

        mgr = McpManager.__new__(McpManager)
        mgr._instances = {
            "browser_ws1": MagicMock(server_name="browser"),
            "browser_ws2": MagicMock(server_name="browser"),
        }
        mgr._plugin_trust_defaults = {
            "browser": ServiceTrustConfig(public_source=True, secret_data=False),
        }

        trust_map = mgr._build_trust_map()
        assert trust_map["browser_ws1"]["public_source"] is True
        assert trust_map["browser_ws2"]["public_source"] is True
