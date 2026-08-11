"""Public Discord channel resolution and thread lifecycle edges."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel
from tests.discord_channel_support import (
    DISCORD_BOT_ENV,
    DISCORD_BOT_VALUE,
    _channel,
    _DirectMessageClient,
    _FakeDiscordGuild,
    _FakeDiscordTextChannel,
    _FakeDiscordUser,
    _FakeThreadParent,
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
async def test_resolve_channel_requires_a_connected_client() -> None:
    with pytest.raises(RuntimeError, match="client is not connected"):
        await _channel().resolve_channel("discord:channel:42")


@pytest.mark.asyncio
async def test_conversation_exists_probes_discord_provider() -> None:
    channel = _channel()
    client = MagicMock()
    client.fetch_channel = AsyncMock(return_value=object())
    channel.client = client

    assert await channel.conversation_exists("discord:channel:42") is True
    client.fetch_channel.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_conversation_exists_treats_archived_thread_as_retired() -> None:
    channel = _channel()
    archived = MagicMock(archived=True)
    client = MagicMock()
    client.fetch_channel = AsyncMock(return_value=archived)
    channel.client = client

    assert await channel.conversation_exists("discord:channel:42") is False


@pytest.mark.asyncio
async def test_conversation_exists_probes_direct_discord_user() -> None:
    channel = _channel()
    client = MagicMock()
    client.fetch_user = AsyncMock(return_value=object())
    channel.client = client

    assert await channel.conversation_exists("discord:direct:42") is True
    client.fetch_user.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_conversation_exists_reports_unknown_channel_as_deleted() -> None:
    channel = _channel()
    client = MagicMock()
    client.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(
            MagicMock(status=404, reason="Not Found"),
            {"code": 10003, "message": "Unknown Channel"},
        )
    )
    channel.client = client

    assert await channel.conversation_exists("discord:channel:42") is False


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
async def test_resolve_chat_jid_prefers_a_cached_configured_channel() -> None:
    channel = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"123": {"channels": {"456": {"enabled": True}}}},
        )
    )
    guild = _FakeDiscordGuild(123, "Pynchy", [])
    cached = _FakeDiscordTextChannel(456, "general")
    client = MagicMock()
    client.get_guild.return_value = guild
    client.get_channel.return_value = cached
    channel.client = client

    assert await channel.resolve_chat_jid("123.channels.456") == "discord:channel:456"
    client.fetch_guild.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_chat_jid_skips_nonmatching_channels() -> None:
    channel = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"123": {"channels": {"456": {"enabled": True}}}},
        )
    )
    guild = _FakeDiscordGuild(
        123,
        "Pynchy",
        [_FakeDiscordTextChannel(999, "other"), _FakeDiscordTextChannel(456, "general")],
    )
    client = MagicMock()
    client.get_guild.return_value = guild
    client.get_channel.return_value = None
    channel.client = client

    assert await channel.resolve_chat_jid("123.channels.456") == "discord:channel:456"


@pytest.mark.asyncio
async def test_resolve_chat_jid_uses_connected_client_for_numeric_direct_ref() -> None:
    channel = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            allow_from=["discord:42"],
            group_policy="disabled",
        )
    )
    channel.client = _DirectMessageClient(
        get_user=lambda user_id: _FakeDiscordUser(user_id, "asmith"),
        fetch_user=None,
    )

    assert await channel.resolve_chat_jid("direct.42") == "discord:direct:42"


@pytest.mark.asyncio
async def test_resolve_chat_jid_finds_direct_name_in_guild_members_without_bulk_helper() -> None:
    channel = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            allow_from=["alice"],
            group_policy="disabled",
        )
    )
    user = _FakeDiscordUser(42, "asmith", display_name="Alice")

    class _ClientWithoutBulkMemberHelper:
        def __init__(self) -> None:
            self.guilds = [_FakeDiscordGuild(1, "Guild", [], members=[user])]
            self.users: list[object] = []

    channel.client = _ClientWithoutBulkMemberHelper()

    assert await channel.resolve_chat_jid("direct.alice") == "discord:direct:42"


@pytest.mark.asyncio
async def test_resolve_chat_jid_rejects_ambiguous_stored_direct_name() -> None:
    async def find_chat_jids(_name: str) -> list[str]:
        await asyncio.sleep(0)
        return ["discord:direct:41", "discord:direct:42"]

    channel = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            allow_from=["alice"],
            group_policy="disabled",
        ).to_runtime_settings(),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, _message: None,
        on_chat_metadata=lambda _jid, _timestamp, _name: None,
        audio_cache_dir=Path("data/media/discord"),
        find_chat_jids_by_name=find_chat_jids,
    )

    assert await channel.resolve_chat_jid("direct.alice") is None


@pytest.mark.asyncio
async def test_resolve_chat_jid_returns_none_for_named_direct_ref_without_client() -> None:
    channel = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            allow_from=["alice"],
            group_policy="disabled",
        ),
        find_chat_jids_by_name=AsyncMock(return_value=[]),
    )

    assert await channel.resolve_chat_jid("direct.alice") is None


@pytest.mark.asyncio
async def test_resolve_chat_jid_rejects_ambiguous_stored_direct_matches() -> None:
    channel = _channel(
        find_chat_jids_by_name=AsyncMock(return_value=["discord:direct:1", "discord:direct:2"])
    )

    assert await channel.resolve_chat_jid("direct.alice") is None


@pytest.mark.asyncio
async def test_create_thread_rejects_target_without_thread_support() -> None:
    channel = _channel()
    channel.resolve_channel = AsyncMock(return_value=object())  # type: ignore[method-assign]

    with pytest.raises(TypeError, match="does not support child threads"):
        await channel.create_thread("discord:channel:123", "family")


@pytest.mark.asyncio
async def test_create_thread_succeeds_when_parent_cannot_announce() -> None:
    parent = _FakeThreadParent()
    parent.send = None  # type: ignore[method-assign]
    channel = _channel()
    channel.resolve_channel = AsyncMock(return_value=parent)  # type: ignore[method-assign]

    assert await channel.create_thread("discord:channel:123", "family") == "discord:channel:456"


@pytest.mark.asyncio
async def test_thread_participant_failure_does_not_fail_existing_thread() -> None:
    thread = MagicMock(id=456)
    thread.add_user = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.HTTPException(MagicMock(status=500, reason="offline"), "offline")
    )
    channel = _channel()
    channel.resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]

    await channel.add_thread_participants("discord:channel:456", ("42",))

    thread.add_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_thread_reuses_archived_thread_without_edit_support() -> None:
    class _ArchivedThread:
        id = 456
        name = "family"
        parent_id = 123
        archived = True

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
async def test_find_thread_returns_none_when_no_active_or_archived_match_exists() -> None:
    channel = _channel()
    channel.resolve_channel = AsyncMock(return_value=_FakeThreadParent())  # type: ignore[method-assign]

    assert await channel.find_thread("discord:channel:123", "family") is None


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
async def test_find_thread_returns_none_without_archived_thread_listing_support() -> None:
    class _Guild:
        async def active_threads(self) -> list[object]:
            return []

    class _Parent:
        id = 123
        guild = _Guild()

    channel = _channel()
    channel.resolve_channel = AsyncMock(return_value=_Parent())  # type: ignore[method-assign]

    assert await channel.find_thread("discord:channel:123", "family") is None


@pytest.mark.asyncio
async def test_create_thread_survives_a_failed_announcement() -> None:
    parent = _FakeThreadParent()
    parent.send = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.HTTPException(MagicMock(status=500, reason="offline"), "offline")
    )
    channel = _channel()
    channel.resolve_channel = AsyncMock(return_value=parent)  # type: ignore[method-assign]

    assert await channel.create_thread("discord:channel:123", "family") == "discord:channel:456"
    assert parent.created_threads[0].id == 456


@pytest.mark.asyncio
async def test_set_thread_closed_rejects_a_non_thread_target() -> None:
    channel = _channel()
    channel.resolve_channel = AsyncMock(return_value=object())  # type: ignore[method-assign]

    with pytest.raises(TypeError, match="thread lifecycle"):
        await channel.set_thread_closed("discord:channel:456", closed=True)
