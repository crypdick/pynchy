"""Integration test for first-party in-repo plugins.

Validates that the in-repo plugin discovery system correctly loads plugins
from their subsystem packages and wires up hook functionality.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pynchy.plugins import get_plugin_manager


class TestInRepoPluginDiscovery:
    """Verify in-repo plugins are discovered via the static registry."""

    def test_all_builtin_plugins_registered(self):
        """All expected built-in plugins appear in the plugin manager."""
        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            pm = get_plugin_manager()

        names = [pm.get_name(p) for p in pm.get_plugins()]
        assert "builtin-claude" in names
        assert "builtin-openai" in names
        assert "builtin-tailscale" in names
        # Slack, WhatsApp, CalDAV, Apple runtime may be skipped
        # due to optional deps — just ensure no errors

    def test_agent_cores_available(self):
        """Both agent cores are discovered and return correct info."""
        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            pm = get_plugin_manager()

        cores = pm.hook.pynchy_agent_core_info()
        names = [c["name"] for c in cores]
        assert "claude" in names
        assert "openai" in names

        claude = next(c for c in cores if c["name"] == "claude")
        assert claude["module"] == "agent_runner.cores.claude"

    def test_tunnel_plugin_available(self):
        """Tailscale tunnel plugin provides a valid provider."""
        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            pm = get_plugin_manager()

        results = pm.hook.pynchy_tunnel()
        assert len(results) >= 1
        tailscale = next((r for r in results if getattr(r, "name", None) == "tailscale"), None)
        assert tailscale is not None

    def test_disabled_plugin_skipped(self):
        """Plugin disabled via config.toml is not loaded."""
        from types import SimpleNamespace

        from pynchy.config import PluginConfig

        settings = SimpleNamespace(
            plugins={"claude": PluginConfig(enabled=False)},
        )

        with (
            patch("pynchy.plugins.registry.get_settings", return_value=settings),
            patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0),
        ):
            pm = get_plugin_manager()

        names = [pm.get_name(p) for p in pm.get_plugins()]
        assert "builtin-claude" not in names
        # Other plugins should still be loaded
        assert "builtin-openai" in names


@pytest.mark.asyncio
class TestSlackPluginFunctionality:
    """Verify Slack plugin hook behavior when loaded."""

    async def test_slack_returns_none_without_tokens(self):
        """Slack plugin returns None when no tokens are configured."""
        from unittest.mock import MagicMock

        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            pm = get_plugin_manager()

        # Check if slack plugin is loaded (optional dep)
        names = [pm.get_name(p) for p in pm.get_plugins()]
        if "builtin-slack" not in names:
            pytest.skip("Slack plugin not available (optional dependency)")

        mock_settings = MagicMock()
        mock_settings.connection.slack = {}

        with patch("pynchy.chat.plugins.slack.get_settings", return_value=mock_settings):
            channels = pm.hook.pynchy_create_channel(context=MagicMock())

        # Slack should return None when no connections configured
        slack_channels = [
            ch
            for ch in channels
            if ch is not None and str(getattr(ch, "name", "")).startswith("connection.slack.")
        ]
        assert len(slack_channels) == 0
