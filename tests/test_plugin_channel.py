"""Tests for channel plugin system."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pynchy.plugin import ChannelPlugin, PluginContext
from pynchy.types import RegisteredGroup


class MockChannel:
    """Mock channel for testing."""

    def __init__(self, name: str):
        self.name = name
        self._connected = False
        self.sent_messages: list[tuple[str, str]] = []

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


class MockChannelPlugin(ChannelPlugin):
    """Mock channel plugin for testing."""

    def __init__(
        self,
        name: str = "test-channel",
        requires_creds: list[str] | None = None,
    ):
        self.name = name
        self.version = "0.1.0"
        self.categories = ["channel"]
        self.description = "Test channel plugin"
        self._requires_creds = requires_creds or []
        self.created_channel: MockChannel | None = None

    def create_channel(self, ctx: PluginContext) -> MockChannel:
        self.created_channel = MockChannel(self.name)
        return self.created_channel

    def requires_credentials(self) -> list[str]:
        return self._requires_creds


class TestChannelPlugin:
    """Tests for ChannelPlugin base class."""

    def test_channel_plugin_has_fixed_category(self):
        """ChannelPlugin has 'channel' as fixed category."""
        plugin = MockChannelPlugin()
        assert plugin.categories == ["channel"]

    def test_create_channel_is_abstract(self):
        """create_channel must be implemented by subclasses."""
        # This is verified by the ABC mechanism, but we test the mock works
        plugin = MockChannelPlugin()
        ctx = PluginContext(
            registered_groups=lambda: {},
            send_message=AsyncMock(),
        )
        channel = plugin.create_channel(ctx)
        assert isinstance(channel, MockChannel)

    def test_requires_credentials_defaults_to_empty(self):
        """requires_credentials returns empty list by default."""
        plugin = MockChannelPlugin()
        assert plugin.requires_credentials() == []

    def test_requires_credentials_can_be_overridden(self):
        """Plugin can specify required credentials."""
        plugin = MockChannelPlugin(requires_creds=["API_TOKEN", "API_SECRET"])
        assert plugin.requires_credentials() == ["API_TOKEN", "API_SECRET"]


class TestPluginContext:
    """Tests for PluginContext dataclass."""

    def test_plugin_context_has_registered_groups(self):
        """PluginContext provides access to registered groups."""
        groups = {
            "test": RegisteredGroup(
                name="Test", folder="test", trigger="@test", added_at="2024-01-01T00:00:00Z"
            )
        }
        ctx = PluginContext(
            registered_groups=lambda: groups,
            send_message=AsyncMock(),
        )
        assert ctx.registered_groups() == groups

    @pytest.mark.asyncio
    async def test_plugin_context_has_send_message(self):
        """PluginContext provides send_message function."""
        send_mock = AsyncMock()
        ctx = PluginContext(
            registered_groups=lambda: {},
            send_message=send_mock,
        )

        await ctx.send_message("test-jid", "test message")
        send_mock.assert_called_once_with("test-jid", "test message")


class TestChannelPluginIntegration:
    """Integration tests for channel plugins."""

    @pytest.mark.asyncio
    async def test_channel_plugin_creates_and_connects_channel(self):
        """Plugin can create a channel that connects successfully."""
        plugin = MockChannelPlugin(name="telegram")
        ctx = PluginContext(
            registered_groups=lambda: {},
            send_message=AsyncMock(),
        )

        channel = plugin.create_channel(ctx)
        assert channel.name == "telegram"
        assert not channel.is_connected()

        await channel.connect()
        assert channel.is_connected()

    @pytest.mark.asyncio
    async def test_channel_can_send_messages(self):
        """Channel created by plugin can send messages."""
        plugin = MockChannelPlugin()
        ctx = PluginContext(
            registered_groups=lambda: {},
            send_message=AsyncMock(),
        )

        channel = plugin.create_channel(ctx)
        await channel.connect()
        await channel.send_message("test-jid", "Hello")

        assert len(channel.sent_messages) == 1
        assert channel.sent_messages[0] == ("test-jid", "Hello")

    @pytest.mark.asyncio
    async def test_channel_owns_jid_routing(self):
        """Channel can identify JIDs it owns."""
        plugin = MockChannelPlugin(name="telegram")
        ctx = PluginContext(
            registered_groups=lambda: {},
            send_message=AsyncMock(),
        )

        channel = plugin.create_channel(ctx)
        assert channel.owns_jid("telegram:123456")
        assert not channel.owns_jid("whatsapp:123456")

    @pytest.mark.asyncio
    async def test_multiple_channel_plugins(self):
        """Multiple channel plugins can coexist."""
        plugin1 = MockChannelPlugin(name="telegram")
        plugin2 = MockChannelPlugin(name="slack")
        ctx = PluginContext(
            registered_groups=lambda: {},
            send_message=AsyncMock(),
        )

        channel1 = plugin1.create_channel(ctx)
        channel2 = plugin2.create_channel(ctx)

        await channel1.connect()
        await channel2.connect()

        assert channel1.is_connected()
        assert channel2.is_connected()
        assert channel1.name != channel2.name


class TestCredentialValidation:
    """Tests for credential validation."""

    def test_validate_no_required_credentials(self, monkeypatch):
        """Plugin with no required credentials passes validation."""
        plugin = MockChannelPlugin()

        # Simulate PynchyApp._validate_plugin_credentials
        import os

        required = plugin.requires_credentials()
        missing = [cred for cred in required if cred not in os.environ]

        assert missing == []

    def test_validate_missing_credentials(self, monkeypatch):
        """Plugin with missing credentials is detected."""
        plugin = MockChannelPlugin(requires_creds=["TELEGRAM_TOKEN", "TELEGRAM_API_ID"])
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_API_ID", raising=False)

        # Simulate PynchyApp._validate_plugin_credentials
        import os

        required = plugin.requires_credentials()
        missing = [cred for cred in required if cred not in os.environ]

        assert set(missing) == {"TELEGRAM_TOKEN", "TELEGRAM_API_ID"}

    def test_validate_partial_credentials(self, monkeypatch):
        """Plugin with some missing credentials is detected."""
        plugin = MockChannelPlugin(requires_creds=["TELEGRAM_TOKEN", "TELEGRAM_API_ID"])
        monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
        monkeypatch.delenv("TELEGRAM_API_ID", raising=False)

        # Simulate PynchyApp._validate_plugin_credentials
        import os

        required = plugin.requires_credentials()
        missing = [cred for cred in required if cred not in os.environ]

        assert missing == ["TELEGRAM_API_ID"]

    def test_validate_all_credentials_present(self, monkeypatch):
        """Plugin with all credentials present passes validation."""
        plugin = MockChannelPlugin(requires_creds=["TELEGRAM_TOKEN", "TELEGRAM_API_ID"])
        monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_API_ID", "test-id")

        # Simulate PynchyApp._validate_plugin_credentials
        import os

        required = plugin.requires_credentials()
        missing = [cred for cred in required if cred not in os.environ]

        assert missing == []
