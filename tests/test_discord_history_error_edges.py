"""Public Discord history behavior when the channel is unavailable."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel
from tests.discord_channel_support import DISCORD_BOT_ENV, _channel


@pytest.mark.asyncio
async def test_resolve_channel_reuses_a_cached_guild_channel() -> None:
    channel = _channel()
    cached = object()
    client = MagicMock()
    client.get_channel.return_value = cached
    client.fetch_channel = AsyncMock()
    channel.client = client

    assert await channel.resolve_channel("discord:channel:42") is cached
    client.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_chat_jid_returns_none_for_named_direct_ref_without_client() -> None:
    channel = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            allow_from=["alice"],
            group_policy="disabled",
        ),
        bot_token=DISCORD_BOT_ENV,
        on_message=lambda _jid, _message: None,
        on_chat_metadata=lambda _jid, _timestamp, _name: None,
        audio_cache_dir=Path("data/media/discord"),
    )

    assert await channel.resolve_chat_jid("direct.alice") is None


@pytest.mark.asyncio
async def test_fetch_inbound_since_returns_no_messages_when_channel_fetch_fails() -> None:
    channel = _channel()
    channel.client = object()
    channel.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.DiscordException("channel unavailable")
    )

    result = await channel.fetch_inbound_since("discord:channel:1", "2026-07-06T00:00:00+00:00")

    assert result.messages == []
    assert not result.high_water_mark
