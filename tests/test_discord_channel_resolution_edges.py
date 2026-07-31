"""Public Discord channel resolution and thread lifecycle edges."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from pynchy.config.api import DiscordConnectionConfig
from tests.discord_channel_support import (
    DISCORD_BOT_ENV,
    _channel,
    _FakeDiscordGuild,
    _FakeDiscordTextChannel,
)


@pytest.mark.asyncio
async def test_resolve_channel_fetches_an_uncached_guild_channel() -> None:
    channel = _channel()
    fetched = object()
    client = MagicMock()
    client.get_channel.return_value = None
    client.fetch_channel = AsyncMock(return_value=fetched)
    channel.client = client

    assert await channel.resolve_channel("discord:channel:42") is fetched
    client.fetch_channel.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_resolve_chat_jid_fetches_a_configured_guild_when_not_cached() -> None:
    channel = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"123": {"channels": {"456": {"enabled": True}}}},
        )
    )
    guild = _FakeDiscordGuild(123, "Pynchy", [_FakeDiscordTextChannel(456, "general")])
    client = MagicMock()
    client.get_guild.return_value = None
    client.fetch_guild = AsyncMock(return_value=guild)
    client.get_channel.return_value = None
    channel.client = client

    assert await channel.resolve_chat_jid("123.channels.456") == "discord:channel:456"
    client.fetch_guild.assert_awaited_once_with(123)


@pytest.mark.asyncio
async def test_find_thread_reuses_archived_thread_when_reopen_fails() -> None:
    class _ArchivedThread:
        id = 456
        name = "family"
        parent_id = 123
        archived = True

        async def edit(self, **_kwargs: object) -> None:
            raise discord.HTTPException(MagicMock(status=500, reason="offline"), "offline")

    class _Guild:
        async def active_threads(self) -> list[object]:
            return []

    class _Parent:
        id = 123
        guild = _Guild()

        async def archived_threads(self, **_kwargs: object):
            yield _ArchivedThread()

    channel = _channel()
    channel.resolve_channel = AsyncMock(return_value=_Parent())  # type: ignore[method-assign]

    assert await channel.find_thread("discord:channel:123", "family") == "discord:channel:456"


@pytest.mark.asyncio
async def test_find_thread_returns_none_without_thread_listing_support() -> None:
    class _Guild:
        pass

    class _Parent:
        id = 123
        guild = _Guild()

    channel = _channel()
    channel.resolve_channel = AsyncMock(return_value=_Parent())  # type: ignore[method-assign]

    assert await channel.find_thread("discord:channel:123", "family") is None


@pytest.mark.asyncio
async def test_set_thread_closed_rejects_a_non_thread_target() -> None:
    channel = _channel()
    channel.resolve_channel = AsyncMock(return_value=object())  # type: ignore[method-assign]

    with pytest.raises(TypeError, match="thread lifecycle"):
        await channel.set_thread_closed("discord:channel:456", closed=True)
