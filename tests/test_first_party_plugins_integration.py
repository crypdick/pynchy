"""Integration test for first-party in-repo plugins.

Validates that the in-repo plugin discovery system correctly loads plugins
from their subsystem packages and wires up hook functionality.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.config import PluginConfig
from pynchy.event_bus import AgentTraceEvent, EventBus, MessageEvent
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.observers import attach_observers
from pynchy.plugins.observers.sqlite_observer.observer import SqliteEventObserver


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
        settings = make_settings(plugins={"claude": PluginConfig(enabled=False)})

        with (
            patch("pynchy.plugins.registry.get_settings", return_value=settings),
            patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0),
        ):
            pm = get_plugin_manager()

        names = [pm.get_name(p) for p in pm.get_plugins()]
        assert "builtin-claude" not in names
        # Other plugins should still be loaded
        assert "builtin-openai" in names


class TestObserverPluginRuntimeTypes:
    """Observer plugin entrypoints should accept runtime EventBus instances."""

    def test_attach_observers_accepts_event_bus(self):
        """attach_observers should not crash resolving the EventBus annotation."""
        with patch("pynchy.plugins.collect_hook_results", return_value=[]):
            assert attach_observers(EventBus()) == []

    def test_sqlite_observer_subscribes_to_event_bus(self):
        """SqliteEventObserver.subscribe should accept a real EventBus."""
        observer = SqliteEventObserver()

        observer.subscribe(EventBus())

    @pytest.mark.asyncio
    async def test_sqlite_observer_keeps_only_bounded_trace_action_name(self):
        """SQLite persists a tool name for Cop context but omits tool inputs."""
        bus = EventBus()
        observer = SqliteEventObserver()
        observer.subscribe(bus)

        with patch("pynchy.state.store_event", new_callable=AsyncMock) as mock_store:
            bus.emit(
                MessageEvent(
                    chat_jid="g@g.us",
                    sender_name="bot",
                    content="hello",
                    timestamp="2026-07-07T00:00:00Z",
                    is_bot=True,
                )
            )
            await asyncio.sleep(0)
            mock_store.assert_awaited_once()

            mock_store.reset_mock()
            bus.emit(
                AgentTraceEvent(
                    chat_jid="g@g.us",
                    trace_type="tool_use",
                    data={"tool_name": "Bash", "tool_input": {"command": "date"}},
                )
            )
            await asyncio.sleep(0)
            mock_store.assert_awaited_once_with(
                "agent_trace",
                "g@g.us",
                {"trace_type": "tool_use", "tool_name": "Bash"},
            )


@pytest.mark.asyncio
class TestSlackPluginFunctionality:
    """Verify Slack plugin hook behavior when loaded."""

    async def test_slack_returns_none_without_tokens(self):
        """Slack plugin returns None when no tokens are configured."""
        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            pm = get_plugin_manager()

        # Check if slack plugin is loaded (optional dep)
        names = [pm.get_name(p) for p in pm.get_plugins()]
        if "builtin-slack" not in names:
            pytest.skip("Slack plugin not available (optional dependency)")

        mock_settings = MagicMock()
        mock_settings.connection.slack = {}

        with patch("pynchy.plugins.channels.slack.get_settings", return_value=mock_settings):
            channels = pm.hook.pynchy_create_channel(context=MagicMock())

        # Slack should return None when no connections configured
        slack_channels = [
            ch
            for ch in channels
            if ch is not None and str(getattr(ch, "name", "")).startswith("connection.slack.")
        ]
        assert len(slack_channels) == 0
