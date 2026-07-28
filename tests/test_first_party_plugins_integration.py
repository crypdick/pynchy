"""Integration test for first-party in-repo plugins.

Validates that the in-repo plugin discovery system correctly loads plugins
from their subsystem packages and wires up hook functionality.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.event_bus import AgentTraceEvent, EventBus
from pynchy.host.orchestrator.plugin_configuration import configure_observer_plugins
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.api import ChannelPluginContext, attach_observers
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
        names = [core.name for core in cores]
        assert "claude" in names
        assert "openai" in names

        claude = next(core for core in cores if core.name == "claude")
        assert claude.module == "agent_runner.cores.claude"

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
        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            pm = get_plugin_manager({"claude": False})

        names = [pm.get_name(p) for p in pm.get_plugins()]
        assert "builtin-claude" not in names
        # Other plugins should still be loaded
        assert "builtin-openai" in names


class TestObserverPluginRuntimeTypes:
    """Observer plugin entrypoints should accept runtime EventBus instances."""

    def test_attach_observers_accepts_event_bus(self):
        """attach_observers should not crash resolving the EventBus annotation."""
        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            plugin_manager = get_plugin_manager({"sqlite-observer": False})

        assert attach_observers(plugin_manager, EventBus()) == []

    def test_builtin_sqlite_observer_is_configured_and_attached(self):
        """Startup composition should attach the registered SQLite observer."""
        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            plugin_manager = get_plugin_manager()

        configure_observer_plugins(plugin_manager)

        assert isinstance(attach_observers(plugin_manager, EventBus())[0], SqliteEventObserver)

    def test_sqlite_observer_subscribes_to_event_bus(self):
        """SqliteEventObserver.subscribe should accept a real EventBus."""
        observer = SqliteEventObserver(store_event=AsyncMock())

        observer.subscribe(EventBus())

    @pytest.mark.asyncio
    async def test_sqlite_observer_persists_bounded_trace_evidence(self):
        """SQLite keeps useful trace evidence without persisting detected private data."""
        bus = EventBus()
        mock_store = AsyncMock()
        observer = SqliteEventObserver(store_event=mock_store)
        observer.subscribe(bus)

        with patch("pynchy.state.api.store_event", new_callable=AsyncMock):
            bus.emit(
                AgentTraceEvent(
                    chat_jid="g@g.us",
                    trace_type="tool_use",
                    data={
                        "tool_name": "Bash",
                        "tool_input": {
                            "command": "read account for private@example.test",
                            "cwd": "/workspace/project",
                        },
                    },
                )
            )
            await asyncio.sleep(0)
            mock_store.assert_awaited_once_with(
                "agent_trace",
                "g@g.us",
                {
                    "trace_type": "tool_use",
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": "[redacted sensitive data: email]",
                        "cwd": "/workspace/project",
                    },
                },
            )

            mock_store.reset_mock()
            credential = "-".join(("xoxb", "1" * 12, "2" * 12, "a" * 24))
            bus.emit(
                AgentTraceEvent(
                    chat_jid="g@g.us",
                    trace_type="tool_result",
                    data={"tool_use_id": "call-2", "content": credential, "is_error": False},
                )
            )
            await asyncio.sleep(0)
            secret_payload = mock_store.await_args.args[2]
            assert secret_payload["content"] == "[redacted secret-bearing trace content]"
            assert credential not in str(secret_payload)

            mock_store.reset_mock()
            bus.emit(
                AgentTraceEvent(
                    chat_jid="g@g.us",
                    trace_type="tool_result",
                    data={"tool_use_id": "call-1", "content": "ok", "is_error": False},
                )
            )
            await asyncio.sleep(0)
            mock_store.assert_awaited_once_with(
                "agent_trace",
                "g@g.us",
                {
                    "trace_type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "ok",
                    "is_error": False,
                },
            )

            mock_store.reset_mock()
            bus.emit(
                AgentTraceEvent(
                    chat_jid="g@g.us",
                    trace_type="text",
                    data={"text": "final answer" + ("x" * 8_000)},
                )
            )
            await asyncio.sleep(0)
            payload = mock_store.await_args.args[2]
            assert payload["trace_type"] == "text"
            assert str(payload["text"]).startswith("final answer")
            assert len(str(payload["text"])) == 6_000


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

        channels = pm.hook.pynchy_create_channel(
            context=ChannelPluginContext(
                on_message_callback=MagicMock(),
                on_chat_metadata_callback=MagicMock(),
                workspaces=MagicMock(return_value={}),
                send_message=MagicMock(),
            )
        )

        # Slack should return None when no connections configured
        slack_channels = [
            ch
            for ch in channels
            if ch is not None and str(getattr(ch, "name", "")).startswith("connection.slack.")
        ]
        assert len(slack_channels) == 0
