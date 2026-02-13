"""Integration tests for ChannelPlugin with PynchyApp."""

from __future__ import annotations

import pytest

from pynchy.app import PynchyApp
from pynchy.plugin import ChannelPlugin, PluginContext
from pynchy.plugin.base import PluginRegistry
from pynchy.types import RegisteredGroup


class MockIntegrationChannel:
    """Mock channel for app integration testing."""

    def __init__(self, name: str, on_message=None, registered_groups=None):
        self.name = name
        self.prefix_assistant_name = False
        self._connected = False
        self.sent_messages: list[tuple[str, str]] = []
        self.on_message = on_message
        self.registered_groups_fn = registered_groups

    async def connect(self) -> None:
        self._connected = True

    async def send_message(self, jid: str, text: str) -> None:
        self.sent_messages.append((jid, text))

    async def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith(f"{self.name}:")


class MockIntegrationPlugin(ChannelPlugin):
    """Mock plugin for integration testing."""

    def __init__(self, name: str = "test-channel", requires_creds: list[str] | None = None):
        self.name = name
        self.version = "0.1.0"
        self.categories = ["channel"]
        self.description = "Test integration plugin"
        self._requires_creds = requires_creds or []
        self.created_channel: MockIntegrationChannel | None = None
        self.context_received: PluginContext | None = None

    def create_channel(self, ctx: PluginContext) -> MockIntegrationChannel:
        self.context_received = ctx
        self.created_channel = MockIntegrationChannel(self.name)
        return self.created_channel

    def requires_credentials(self) -> list[str]:
        return self._requires_creds


class TestAppChannelPluginIntegration:
    """Tests for ChannelPlugin integration with PynchyApp."""

    def test_app_initializes_with_registry(self):
        """PynchyApp can be initialized with a registry attribute."""
        app = PynchyApp()
        assert hasattr(app, "registry")
        assert app.registry is None  # Not set until run()

    def test_validate_plugin_credentials_no_requirements(self):
        """_validate_plugin_credentials returns empty list for no requirements."""
        app = PynchyApp()
        plugin = MockIntegrationPlugin()

        missing = app._validate_plugin_credentials(plugin)
        assert missing == []

    def test_validate_plugin_credentials_missing(self, monkeypatch):
        """_validate_plugin_credentials detects missing credentials."""
        app = PynchyApp()
        plugin = MockIntegrationPlugin(requires_creds=["TEST_TOKEN", "TEST_API_KEY"])
        monkeypatch.delenv("TEST_TOKEN", raising=False)
        monkeypatch.delenv("TEST_API_KEY", raising=False)

        missing = app._validate_plugin_credentials(plugin)
        assert set(missing) == {"TEST_TOKEN", "TEST_API_KEY"}

    def test_validate_plugin_credentials_present(self, monkeypatch):
        """_validate_plugin_credentials passes when all credentials present."""
        app = PynchyApp()
        plugin = MockIntegrationPlugin(requires_creds=["TEST_TOKEN"])
        monkeypatch.setenv("TEST_TOKEN", "test-value")

        missing = app._validate_plugin_credentials(plugin)
        assert missing == []

    @pytest.mark.asyncio
    async def test_connect_plugin_channels_with_no_plugins(self):
        """_connect_plugin_channels handles no plugins gracefully."""
        app = PynchyApp()
        app.registry = PluginRegistry()  # Empty registry
        app.registered_groups = {}

        # Should not raise
        await app._connect_plugin_channels()

    @pytest.mark.asyncio
    async def test_connect_plugin_channels_creates_and_connects(self):
        """_connect_plugin_channels creates channel from plugin and connects it."""
        app = PynchyApp()
        plugin = MockIntegrationPlugin(name="telegram")

        # Create registry with plugin
        registry = PluginRegistry()
        registry.channels.append(plugin)
        app.registry = registry
        app.registered_groups = {}

        await app._connect_plugin_channels()

        # Verify channel was created and added
        assert len(app.channels) == 1
        channel = app.channels[0]
        assert channel.name == "telegram"
        assert channel.is_connected()

    @pytest.mark.asyncio
    async def test_connect_plugin_channels_provides_context(self):
        """_connect_plugin_channels provides PluginContext to plugins."""
        app = PynchyApp()
        plugin = MockIntegrationPlugin()

        registry = PluginRegistry()
        registry.channels.append(plugin)
        app.registry = registry
        app.registered_groups = {
            "test-jid": RegisteredGroup(
                name="Test", folder="test", trigger="@test", added_at="2024-01-01T00:00:00Z"
            )
        }

        await app._connect_plugin_channels()

        # Verify plugin received context
        assert plugin.context_received is not None
        groups = plugin.context_received.registered_groups()
        assert "test-jid" in groups
        assert groups["test-jid"].name == "Test"

    @pytest.mark.asyncio
    async def test_connect_plugin_channels_skips_missing_credentials(self, monkeypatch):
        """_connect_plugin_channels skips plugins with missing credentials."""
        app = PynchyApp()
        plugin = MockIntegrationPlugin(requires_creds=["MISSING_TOKEN"])
        monkeypatch.delenv("MISSING_TOKEN", raising=False)

        registry = PluginRegistry()
        registry.channels.append(plugin)
        app.registry = registry
        app.registered_groups = {}

        await app._connect_plugin_channels()

        # Channel should not be created
        assert len(app.channels) == 0
        assert plugin.created_channel is None

    @pytest.mark.asyncio
    async def test_connect_plugin_channels_handles_plugin_errors(self):
        """_connect_plugin_channels continues on plugin errors."""

        class BrokenPlugin(ChannelPlugin):
            name = "broken"
            version = "0.1.0"
            description = "Broken plugin"
            categories = ["channel"]

            def create_channel(self, ctx: PluginContext) -> None:
                msg = "Plugin initialization failed"
                raise RuntimeError(msg)

        app = PynchyApp()
        good_plugin = MockIntegrationPlugin(name="good")
        broken_plugin = BrokenPlugin()

        registry = PluginRegistry()
        registry.channels.append(good_plugin)
        registry.channels.append(broken_plugin)
        app.registry = registry
        app.registered_groups = {}

        # Should not raise - continues with good plugins
        await app._connect_plugin_channels()

        # Only good plugin's channel should be added
        assert len(app.channels) == 1
        assert app.channels[0].name == "good"

    @pytest.mark.asyncio
    async def test_connect_multiple_plugin_channels(self):
        """_connect_plugin_channels can connect multiple plugins."""
        app = PynchyApp()
        plugin1 = MockIntegrationPlugin(name="telegram")
        plugin2 = MockIntegrationPlugin(name="slack")

        registry = PluginRegistry()
        registry.channels.extend([plugin1, plugin2])
        app.registry = registry
        app.registered_groups = {}

        await app._connect_plugin_channels()

        # Both channels should be added and connected
        assert len(app.channels) == 2
        assert {ch.name for ch in app.channels} == {"telegram", "slack"}
        assert all(ch.is_connected() for ch in app.channels)

    @pytest.mark.asyncio
    async def test_plugin_send_message_helper(self):
        """_plugin_send_message sends to all connected channels."""
        app = PynchyApp()

        # Create mock channels
        channel1 = MockIntegrationChannel("telegram")
        channel2 = MockIntegrationChannel("slack")
        await channel1.connect()
        await channel2.connect()

        app.channels = [channel1, channel2]

        # Use the helper
        await app._plugin_send_message("test-jid", "test message")

        # Both channels should have received the message
        assert ("test-jid", "test message") in channel1.sent_messages
        assert ("test-jid", "test message") in channel2.sent_messages

    @pytest.mark.asyncio
    async def test_plugin_context_send_message_callback(self):
        """Plugin context send_message callback works."""
        app = PynchyApp()
        plugin = MockIntegrationPlugin()

        # Add a channel to receive messages
        mock_channel = MockIntegrationChannel("whatsapp")
        await mock_channel.connect()
        app.channels = [mock_channel]

        registry = PluginRegistry()
        registry.channels.append(plugin)
        app.registry = registry
        app.registered_groups = {}

        await app._connect_plugin_channels()

        # Plugin should have received context with working send_message
        assert plugin.context_received is not None
        await plugin.context_received.send_message("test-jid", "hello")

        # Message should have been sent through channels
        assert ("test-jid", "hello") in mock_channel.sent_messages


class TestMultiChannelRouting:
    """Tests for multi-channel message routing with plugins."""

    @pytest.mark.asyncio
    async def test_channels_can_own_different_jids(self):
        """Each channel can own different JID patterns."""
        app = PynchyApp()

        whatsapp = MockIntegrationChannel("whatsapp")
        telegram = MockIntegrationChannel("telegram")
        slack = MockIntegrationChannel("slack")

        app.channels = [whatsapp, telegram, slack]

        # Each channel owns its own JID pattern
        assert whatsapp.owns_jid("whatsapp:123")
        assert not whatsapp.owns_jid("telegram:123")

        assert telegram.owns_jid("telegram:456")
        assert not telegram.owns_jid("slack:456")

        assert slack.owns_jid("slack:789")
        assert not slack.owns_jid("whatsapp:789")

    @pytest.mark.asyncio
    async def test_broadcast_to_multiple_channels(self):
        """Messages can be broadcast to multiple channels."""
        app = PynchyApp()

        channel1 = MockIntegrationChannel("telegram")
        channel2 = MockIntegrationChannel("slack")
        await channel1.connect()
        await channel2.connect()

        app.channels = [channel1, channel2]

        # Simulate broadcast
        for ch in app.channels:
            if ch.is_connected():
                await ch.send_message("test-jid", "broadcast message")

        # Both should have received it
        assert ("test-jid", "broadcast message") in channel1.sent_messages
        assert ("test-jid", "broadcast message") in channel2.sent_messages
