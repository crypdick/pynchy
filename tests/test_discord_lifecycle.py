"""Tests for Discord lifecycle state coordination."""

from __future__ import annotations

import pytest

from pynchy.config.models import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel

DISCORD_BOT_ENV = "X"
DISCORD_BOT_VALUE = "token"


def _channel() -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )


class _FakeClosableClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_discord_channel_exposes_lifecycle_state_properties() -> None:
    ch = _channel()

    assert ch.bot_token == DISCORD_BOT_VALUE
    assert ch.connected is False
    assert ch.shutting_down is False

    ch.connected = True
    ch.shutting_down = True

    assert ch.connected is True
    assert ch.shutting_down is True


def test_discord_lifecycle_prepare_shutdown_sets_public_state() -> None:
    ch = _channel()

    ch.lifecycle.prepare_shutdown()

    assert ch.shutting_down is True


@pytest.mark.asyncio
async def test_discord_lifecycle_disconnect_updates_public_state() -> None:
    ch = _channel()
    client = _FakeClosableClient()
    ch.client = client
    ch.connected = True

    await ch.lifecycle.disconnect()

    assert client.closed is True
    assert ch.connected is False
    assert ch.shutting_down is True
