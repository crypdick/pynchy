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


def test_satisfies_channel_protocol():
    assert isinstance(_channel(), Channel)


def test_owns_only_discord_jids():
    ch = _channel()
    assert ch.owns_jid("discord:channel:1") is True
    assert ch.owns_jid("discord:direct:1") is True
    assert ch.owns_jid("slack:C1") is False


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
