"""Tests for DiscordChannel outbound behavior and protocol conformance.

The discord.py client is faked; these cover the channel's own logic (jid
ownership, chunked sending with safe mention defaults, reaction id handling,
history catch-up filtering) rather than the gateway glue in _lifecycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pynchy.config.models import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel, DiscordChannelPlugin
from pynchy.types import Channel, OutboundEvent, OutboundEventType


def _channel() -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(bot_token_env="X"),
        bot_token="token",
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )


class _FakeSendChannel:
    def __init__(self) -> None:
        self.sends: list[tuple[str, dict]] = []

    async def send(self, content: str, **kwargs) -> None:
        self.sends.append((content, kwargs))


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits: list[tuple[str, dict]] = []

    async def edit(self, *, content: str, **kwargs) -> None:
        self.edits.append((content, kwargs))


class _FakeStreamChannel:
    """A channel whose ``send`` returns a message and that can fetch it back."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, dict]] = []
        self.messages: dict[int, _FakeMessage] = {}
        self._next_id = 100

    async def send(self, content: str, **kwargs) -> _FakeMessage:
        self._next_id += 1
        msg = _FakeMessage(self._next_id)
        self.messages[msg.id] = msg
        self.sends.append((content, kwargs))
        return msg

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        return self.messages[message_id]


class _FakeDiscordTextChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name


class _FakeDiscordGuild:
    def __init__(self, guild_id: int, name: str, channels: list[_FakeDiscordTextChannel]) -> None:
        self.id = guild_id
        self.name = name
        self.text_channels = channels
        self.created: list[str] = []

    async def create_text_channel(self, name: str, **kwargs) -> _FakeDiscordTextChannel:
        self.created.append(name)
        channel = _FakeDiscordTextChannel(789, name)
        self.text_channels.append(channel)
        return channel


class _FakeDiscordClient:
    def __init__(self, guilds: list[_FakeDiscordGuild]) -> None:
        self.guilds = guilds

    def get_guild(self, guild_id: int) -> _FakeDiscordGuild | None:
        return next((guild for guild in self.guilds if guild.id == guild_id), None)

    async def fetch_guild(self, guild_id: int) -> _FakeDiscordGuild | None:
        return self.get_guild(guild_id)


def test_satisfies_channel_protocol():
    assert isinstance(_channel(), Channel)


def test_owns_only_discord_jids():
    ch = _channel()
    assert ch.owns_jid("discord:channel:1") is True
    assert ch.owns_jid("discord:direct:1") is True
    assert ch.owns_jid("slack:C1") is False


@pytest.mark.asyncio
async def test_resolve_chat_jid_maps_configured_guild_channel_ref():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env="X",
            dm_policy="allowlist",
            group_policy="allowlist",
            chat={"123": {"require_mention": False, "channels": {"456": {"enabled": True}}}},
        ),
        bot_token="token",
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )

    assert await ch.resolve_chat_jid("123.channels.456") == "discord:channel:456"


@pytest.mark.asyncio
async def test_resolve_chat_jid_maps_allowed_direct_ref():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env="X",
            dm_policy="allowlist",
            allow_from=["discord:42"],
            group_policy="disabled",
        ),
        bot_token="token",
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )

    assert await ch.resolve_chat_jid("direct.42") == "discord:direct:42"


@pytest.mark.asyncio
async def test_resolve_chat_jid_returns_none_for_unconfigured_channel_ref():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env="X",
            dm_policy="allowlist",
            group_policy="allowlist",
            chat={"123": {"require_mention": False, "channels": {}}},
        ),
        bot_token="token",
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )

    assert await ch.resolve_chat_jid("123.channels.456") is None


@pytest.mark.asyncio
async def test_resolve_chat_jid_maps_configured_name_ref_to_existing_channel():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env="X",
            dm_policy="allowlist",
            group_policy="allowlist",
            chat={
                "synapse": {
                    "name": "Synapse",
                    "channels": {"code-improver": {"name": "code-improver"}},
                }
            },
        ),
        bot_token="token",
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )
    ch.client = _FakeDiscordClient(
        [_FakeDiscordGuild(123, "Synapse", [_FakeDiscordTextChannel(456, "code-improver")])]
    )

    assert await ch.resolve_chat_jid("synapse.channels.code-improver") == "discord:channel:456"


@pytest.mark.asyncio
async def test_create_group_creates_named_discord_channel():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env="X",
            dm_policy="allowlist",
            group_policy="allowlist",
            chat={
                "synapse": {
                    "name": "Synapse",
                    "channels": {"code-improver": {"name": "code-improver"}},
                }
            },
        ),
        bot_token="token",
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )
    guild = _FakeDiscordGuild(123, "Synapse", [])
    ch.client = _FakeDiscordClient([guild])

    assert await ch.create_group("synapse.channels.code-improver") == "discord:channel:789"
    assert guild.created == ["code-improver"]


@pytest.mark.asyncio
async def test_send_event_chunks_long_text_with_safe_mentions():
    ch = _channel()
    ch.client = object()  # non-None so the guard passes
    fake = _FakeSendChannel()

    async def _resolve(_jid: str) -> _FakeSendChannel:
        return fake

    ch._resolve_channel = _resolve  # type: ignore[method-assign]

    long_text = "word " * 1000  # ~5000 chars -> multiple chunks
    await ch.send_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content=long_text)
    )

    assert len(fake.sends) > 1
    assert all(len(content) <= 2000 for content, _ in fake.sends)
    # every send suppresses accidental pings
    assert all(kw["allowed_mentions"] is not None for _, kw in fake.sends)


@pytest.mark.asyncio
async def test_send_event_skips_empty_text():
    ch = _channel()
    ch.client = object()
    called = False

    async def _resolve(_jid: str):
        nonlocal called
        called = True
        return _FakeSendChannel()

    ch._resolve_channel = _resolve  # type: ignore[method-assign]
    await ch.send_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="   ")
    )
    assert called is False


@pytest.mark.asyncio
async def test_send_event_ignores_foreign_jid():
    ch = _channel()
    ch.client = object()
    resolved = False

    async def _resolve(_jid: str):
        nonlocal resolved
        resolved = True
        return _FakeSendChannel()

    ch._resolve_channel = _resolve  # type: ignore[method-assign]
    await ch.send_event("slack:C1", OutboundEvent(type=OutboundEventType.TEXT, content="hi"))
    assert resolved is False


@pytest.mark.asyncio
async def test_send_reaction_ignores_non_discord_message_id():
    ch = _channel()
    ch.client = object()

    async def _resolve(_jid: str):
        raise AssertionError("should not resolve for a non-Discord message id")

    ch._resolve_channel = _resolve  # type: ignore[method-assign]
    # slack-style id must be a no-op, not an error
    await ch.send_reaction("discord:channel:1", "slack-123", "u1", "👀")


@pytest.mark.asyncio
async def test_fetch_inbound_since_filters_bot_and_self():
    ch = _channel()
    ch.client = object()
    ch.bot_user_id = "self"

    def _msg(mid: str, author_id: str, *, bot: bool) -> SimpleNamespace:
        return SimpleNamespace(
            id=mid,
            author=SimpleNamespace(id=author_id, bot=bot, display_name=f"user{author_id}"),
            content=f"msg {mid}",
            created_at=datetime(2026, 7, 7, tzinfo=UTC),
        )

    class _HistChannel:
        def history(self, **kwargs):
            async def gen():
                yield _msg("1", "human", bot=False)
                yield _msg("2", "otherbot", bot=True)
                yield _msg("3", "self", bot=False)

            return gen()

    async def _resolve(_jid: str) -> _HistChannel:
        return _HistChannel()

    ch._resolve_channel = _resolve  # type: ignore[method-assign]

    result = await ch.fetch_inbound_since("discord:channel:1", "2026-07-06T00:00:00+00:00")
    ids = [m.id for m in result.messages]
    assert ids == ["discord-1"]  # bot + own filtered out
    assert result.high_water_mark != ""


def test_plugin_returns_none_without_context():
    assert DiscordChannelPlugin().pynchy_create_channel(context=None) is None


# ---------------------------------------------------------------------------
# Streaming (post_event / update_event)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_event_sends_preview_and_returns_message_id():
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()

    async def _resolve(_jid: str) -> _FakeStreamChannel:
        return fake

    ch._resolve_channel = _resolve  # type: ignore[method-assign]
    msg_id = await ch.post_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="hi there")
    )
    assert msg_id == "discord-101"
    assert fake.sends[0][0] == "hi there"
    # streamed previews must also use safe mention defaults
    assert fake.sends[0][1]["allowed_mentions"] is not None


@pytest.mark.asyncio
async def test_post_event_returns_none_for_empty_text():
    ch = _channel()
    ch.client = object()
    ch._resolve_channel = lambda _jid: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("should not resolve for empty text")
    )
    result = await ch.post_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="   ")
    )
    assert result is None


@pytest.mark.asyncio
async def test_post_event_returns_none_when_too_large_to_stream():
    # A message over the single-message limit can't be an editable preview;
    # returning None makes core route it through chunked send_event instead.
    ch = _channel()
    ch.client = object()
    ch._resolve_channel = lambda _jid: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("should not resolve when text exceeds the limit")
    )
    result = await ch.post_event(
        "discord:channel:1",
        OutboundEvent(type=OutboundEventType.TEXT, content="x" * 2001),
    )
    assert result is None


@pytest.mark.asyncio
async def test_update_event_edits_message_in_place():
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()
    msg = await fake.send("initial", allowed_mentions=None)

    async def _resolve(_jid: str) -> _FakeStreamChannel:
        return fake

    ch._resolve_channel = _resolve  # type: ignore[method-assign]
    await ch.update_event(
        "discord:channel:1",
        f"discord-{msg.id}",
        OutboundEvent(type=OutboundEventType.TEXT, content="updated text"),
    )
    assert msg.edits[-1][0] == "updated text"


@pytest.mark.asyncio
async def test_update_event_raises_when_too_large_so_core_falls_back():
    # Discord can't edit a message beyond the limit; raising lets sender.py
    # fall back to chunked send_event.
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()

    async def _resolve(_jid: str) -> _FakeStreamChannel:
        return fake

    ch._resolve_channel = _resolve  # type: ignore[method-assign]
    with pytest.raises(Exception):  # noqa: B017 -- any raise triggers the fallback
        await ch.update_event(
            "discord:channel:1",
            "discord-101",
            OutboundEvent(type=OutboundEventType.TEXT, content="x" * 2001),
        )


@pytest.mark.asyncio
async def test_streaming_channel_satisfies_protocol_and_is_detected():
    ch = _channel()
    # core detects streaming targets via hasattr on both methods
    assert hasattr(ch, "post_event")
    assert hasattr(ch, "update_event")
