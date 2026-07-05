"""Tests for the Playwright browser plugin."""

from __future__ import annotations

from pathlib import Path

from pynchy.plugins.integrations.playwright_browser import PlaywrightBrowserPlugin


class TestMcpServerSpec:
    def test_returns_dict_with_required_fields(self):
        plugin = PlaywrightBrowserPlugin()
        spec = plugin.pynchy_mcp_server_spec()
        assert isinstance(spec, dict)
        assert spec["name"] == "browser"
        assert spec["command"] == "npx"
        assert "@playwright/mcp@latest" in spec["args"][0]
        assert spec["port"] == 9100
        assert spec["transport"] == "streamable_http"

    def test_port_uses_placeholder(self):
        """Port arg uses {port} placeholder — expanded at launch to each instance's port."""
        plugin = PlaywrightBrowserPlugin()
        spec = plugin.pynchy_mcp_server_spec()
        args = spec["args"]
        port_idx = args.index("--port")
        assert args[port_idx + 1] == "{port}"

    def test_trust_defaults_set(self):
        plugin = PlaywrightBrowserPlugin()
        spec = plugin.pynchy_mcp_server_spec()
        trust = spec["trust"]
        assert trust["public_source"] is True
        assert trust["secret_data"] is False
        assert trust["public_sink"] is False
        assert trust["dangerous_writes"] is False


class TestSkillPaths:
    def test_returns_browser_control_skill(self):
        plugin = PlaywrightBrowserPlugin()
        paths = plugin.pynchy_skill_paths()
        assert isinstance(paths, list)
        assert len(paths) >= 1
        skill_path = Path(paths[0])
        assert skill_path.name == "browser-control"


class TestSkillContent:
    def test_skill_md_has_frontmatter(self):
        skill_md = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "pynchy"
            / "agent"
            / "skills"
            / "browser-control"
            / "SKILL.md"
        )
        assert skill_md.exists(), f"Expected skill at {skill_md}"
        content = skill_md.read_text()
        assert content.startswith("---")
        assert "name:" in content
        assert "tier:" in content


class TestPluginRegistration:
    def test_plugin_is_registered(self):
        """The playwright plugin should be registered in the plugin manager."""
        from pynchy.plugins import get_plugin_manager

        pm = get_plugin_manager()
        plugin = pm.get_plugin("builtin-playwright-browser")
        assert isinstance(plugin, PlaywrightBrowserPlugin)
