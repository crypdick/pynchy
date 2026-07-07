"""Tests for extracting inbound context from a Discord message.

``build_inbound_context`` and ``jid_for`` are the pure boundary between
discord.py's message objects and the access/routing layers. They are tested
with duck-typed fakes so no gateway is needed.
"""

from __future__ import annotations

from types import SimpleNamespace

from pynchy.plugins.channels.discord._events import build_inbound_context, jid_for

BOT_ID = "999"


def _user(uid: str, *, bot: bool = False, roles: tuple[str, ...] = ()) -> SimpleNamespace:
    # Real Discord ids are numeric snowflakes; the extraction only ever str()s
    # them, so string ids are fine for these pure-function tests.
    return SimpleNamespace(id=uid, bot=bot, roles=[SimpleNamespace(id=r) for r in roles])


def _message(
    *,
    author: SimpleNamespace,
    guild_id: str | None,
    channel_id: str,
    parent_id: str | None = None,
    mentions: tuple[str, ...] = (),
) -> SimpleNamespace:
    guild = None if guild_id is None else SimpleNamespace(id=guild_id)
    channel = SimpleNamespace(id=channel_id)
    if parent_id is not None:
        channel.parent_id = parent_id
    return SimpleNamespace(
        author=author,
        guild=guild,
        channel=channel,
        mentions=[_user(m) for m in mentions],
    )


def test_dm_context():
    msg = _message(author=_user("1"), guild_id=None, channel_id="dm1")
    ctx = build_inbound_context(msg, BOT_ID)
    assert ctx.is_dm is True
    assert ctx.author_id == "1"
    assert ctx.guild_id is None


def test_guild_context():
    msg = _message(author=_user("7"), guild_id="g1", channel_id="c1")
    ctx = build_inbound_context(msg, BOT_ID)
    assert ctx.is_dm is False
    assert ctx.guild_id == "g1"
    assert ctx.channel_id == "c1"
    assert ctx.parent_channel_id is None


def test_thread_context_carries_parent():
    msg = _message(author=_user("7"), guild_id="g1", channel_id="t1", parent_id="c1")
    ctx = build_inbound_context(msg, BOT_ID)
    assert ctx.channel_id == "t1"
    assert ctx.parent_channel_id == "c1"


def test_bot_author_flagged():
    msg = _message(author=_user("2", bot=True), guild_id="g1", channel_id="c1")
    assert build_inbound_context(msg, BOT_ID).author_is_bot is True


def test_mentions_bot_detected():
    msg = _message(author=_user("7"), guild_id="g1", channel_id="c1", mentions=(BOT_ID,))
    assert build_inbound_context(msg, BOT_ID).mentions_bot is True


def test_mentions_other_not_bot():
    msg = _message(author=_user("7"), guild_id="g1", channel_id="c1", mentions=("42",))
    assert build_inbound_context(msg, BOT_ID).mentions_bot is False


def test_role_ids_extracted():
    msg = _message(author=_user("7", roles=("r1", "r2")), guild_id="g1", channel_id="c1")
    assert build_inbound_context(msg, BOT_ID).author_role_ids == frozenset({"r1", "r2"})


def test_jid_for_dm_keys_off_user():
    msg = _message(author=_user("5"), guild_id=None, channel_id="dm1")
    ctx = build_inbound_context(msg, BOT_ID)
    assert jid_for(ctx) == "discord:direct:5"


def test_jid_for_guild_channel_keys_off_channel():
    msg = _message(author=_user("5"), guild_id="g1", channel_id="c1")
    ctx = build_inbound_context(msg, BOT_ID)
    assert jid_for(ctx) == "discord:channel:c1"


def test_jid_for_thread_uses_thread_snowflake():
    msg = _message(author=_user("5"), guild_id="g1", channel_id="t1", parent_id="c1")
    ctx = build_inbound_context(msg, BOT_ID)
    assert jid_for(ctx) == "discord:channel:t1"
