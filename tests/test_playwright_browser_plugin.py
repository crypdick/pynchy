"""Tests for the Playwright browser plugin."""

from __future__ import annotations

from pathlib import Path

from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations.playwright_browser import PlaywrightBrowserPlugin


class TestMcpServerSpec:
    def test_returns_typed_spec_with_required_fields(self):
        plugin = PlaywrightBrowserPlugin()
        spec = plugin.pynchy_mcp_server_spec()[0]
        assert spec.name == "browser"
        assert spec.config.command == "npx"
        assert "@playwright/mcp@latest" in spec.config.args[0]
        assert spec.config.port == 9100
        assert spec.config.transport == "streamable_http"

    def test_port_uses_placeholder(self):
        """Port arg uses {port} placeholder — expanded at launch to each instance's port."""
        plugin = PlaywrightBrowserPlugin()
        spec = plugin.pynchy_mcp_server_spec()[0]
        args = spec.config.args
        port_idx = args.index("--port")
        assert args[port_idx + 1] == "{port}"

    def test_host_is_loopback_only(self):
        plugin = PlaywrightBrowserPlugin()
        spec = plugin.pynchy_mcp_server_spec()[0]
        args = spec.config.args
        host_idx = args.index("--host")
        assert args[host_idx + 1] == "localhost"

    def test_browser_runs_headed_by_default(self, monkeypatch):
        monkeypatch.delenv("PYNCHY_BROWSER_HEADLESS", raising=False)

        plugin = PlaywrightBrowserPlugin()
        spec = plugin.pynchy_mcp_server_spec()[0]

        assert "--headless" not in spec.config.args

    def test_browser_can_be_forced_headless_for_headless_hosts(self, monkeypatch):
        monkeypatch.setenv("PYNCHY_BROWSER_HEADLESS", "true")

        plugin = PlaywrightBrowserPlugin()
        spec = plugin.pynchy_mcp_server_spec()[0]

        assert "--headless" in spec.config.args

    def test_trust_defaults_set(self):
        plugin = PlaywrightBrowserPlugin()
        spec = plugin.pynchy_mcp_server_spec()[0]
        assert spec.trust is not None
        assert spec.trust.public_source is True
        assert spec.trust.secret_data is False
        assert spec.trust.public_sink is False
        assert spec.trust.dangerous_writes is False


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
        pm = get_plugin_manager()
        plugin = pm.get_plugin("builtin-playwright-browser")
        assert isinstance(plugin, PlaywrightBrowserPlugin)
