"""Tests for extracting inbound context from a Discord message.

``build_inbound_context`` and ``jid_for`` are the pure boundary between
discord.py's message objects and the access/routing layers. They are tested
with duck-typed fakes so no gateway is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from pynchy.plugins.channels.discord._events import (
    build_inbound_context,
    build_message_metadata,
    jid_for,
    normalized_message_content,
)

BOT_ID = "999"


def _user(
    uid: str,
    *,
    bot: bool = False,
    roles: tuple[str, ...] = (),
    display_name: str | None = None,
    global_name: str | None = None,
    name: str | None = None,
) -> SimpleNamespace:
    # Real Discord ids are numeric snowflakes; the extraction only ever str()s
    # them, so string ids are fine for these pure-function tests.
    return SimpleNamespace(
        id=uid,
        bot=bot,
        roles=[SimpleNamespace(id=r) for r in roles],
        display_name=display_name,
        global_name=global_name,
        name=name,
    )


def _message(
    *,
    author: SimpleNamespace,
    guild_id: str | None,
    channel_id: str,
    guild_name: str | None = None,
    channel_name: str | None = None,
    parent_id: str | None = None,
    parent_name: str | None = None,
    mentions: tuple[str, ...] = (),
    content: str = "",
    attachments: tuple[SimpleNamespace, ...] = (),
    reference: SimpleNamespace | None = None,
    message_snapshots: tuple[SimpleNamespace, ...] = (),
) -> SimpleNamespace:
    guild = None if guild_id is None else SimpleNamespace(id=guild_id, name=guild_name)
    parent = None
    if parent_id is not None:
        parent = SimpleNamespace(id=parent_id, name=parent_name)
    channel = SimpleNamespace(id=channel_id, name=channel_name, parent=parent)
    if parent_id is not None:
        channel.parent_id = parent_id
    return SimpleNamespace(
        id="m1",
        author=author,
        guild=guild,
        channel=channel,
        content=content,
        attachments=list(attachments),
        reference=reference,
        message_snapshots=list(message_snapshots),
        mentions=[_user(m) for m in mentions],
    )


def _attachment(
    *,
    attachment_id: str,
    filename: str,
    url: str = "https://example.invalid/file.txt",
    proxy_url: str = "https://cdn.example.invalid/file.txt",
    content_type: str | None = "text/plain",
    size: int = 12,
    description: str | None = None,
    spoiler: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=attachment_id,
        filename=filename,
        url=url,
        proxy_url=proxy_url,
        content_type=content_type,
        size=size,
        description=description,
        spoiler=spoiler,
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


def test_guild_context_carries_names():
    msg = _message(
        author=_user("7"),
        guild_id="g1",
        guild_name="Synapse",
        channel_id="c1",
        channel_name="code-improver",
    )
    ctx = build_inbound_context(msg, BOT_ID)
    assert ctx.guild_name == "Synapse"
    assert ctx.channel_name == "code-improver"


def test_context_carries_author_names():
    msg = _message(
        author=_user("7", display_name="Ricardo", global_name="rdecal", name="ricardo-local"),
        guild_id="g1",
        channel_id="c1",
    )

    assert {"Ricardo", "rdecal", "ricardo-local"} <= build_inbound_context(msg, BOT_ID).author_names


def test_thread_context_carries_parent():
    msg = _message(
        author=_user("7"),
        guild_id="g1",
        channel_id="t1",
        channel_name="run-123",
        parent_id="c1",
        parent_name="code-improver",
    )
    ctx = build_inbound_context(msg, BOT_ID)
    assert ctx.channel_id == "t1"
    assert ctx.parent_channel_id == "c1"
    assert ctx.parent_channel_name == "code-improver"


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


def test_build_message_metadata_for_thread_carries_parent_jid_and_names():
    msg = _message(
        author=_user("5"),
        guild_id="g1",
        channel_id="t1",
        channel_name="run-123",
        parent_id="c1",
        parent_name="admin",
    )
    ctx = build_inbound_context(msg, BOT_ID)

    metadata = build_message_metadata(msg, ctx)

    assert metadata["discord_message_id"] == "m1"
    assert metadata["discord_parent_chat_jid"] == "discord:channel:c1"
    assert metadata["discord_channel_name"] == "run-123"
    assert metadata["discord_parent_channel_name"] == "admin"


def test_build_message_metadata_extracts_reply_context():
    replied_author = SimpleNamespace(id="42", display_name="Alice")
    replied = SimpleNamespace(id="reply-1", author=replied_author, content="Original message")
    reference = SimpleNamespace(message_id="reply-1", resolved=replied)
    msg = _message(
        author=_user("5"),
        guild_id="g1",
        channel_id="c1",
        content="Following up",
        reference=reference,
    )

    metadata = build_message_metadata(msg)

    assert metadata["discord_message_id"] == "m1"
    assert metadata["reply_to_message_id"] == "reply-1"
    assert metadata["reply_to_sender"] == "Alice"
    assert metadata["reply_to_text"] == "Original message"


def test_build_message_metadata_preserves_attachments():
    msg = _message(
        author=_user("5"),
        guild_id="g1",
        channel_id="c1",
        content="See attached",
        attachments=(
            _attachment(
                attachment_id="a1",
                filename="design.txt",
                description="Architecture sketch",
            ),
        ),
    )

    metadata = build_message_metadata(msg)

    assert metadata["attachments"] == [
        {
            "id": "a1",
            "filename": "design.txt",
            "url": "https://example.invalid/file.txt",
            "proxy_url": "https://cdn.example.invalid/file.txt",
            "content_type": "text/plain",
            "size": 12,
            "description": "Architecture sketch",
            "spoiler": False,
        }
    ]


def test_forwarded_snapshot_text_falls_back_when_message_content_missing():
    snapshot = SimpleNamespace(
        type="default",
        content="Forwarded content",
        created_at=datetime(2026, 7, 7, tzinfo=UTC),
        attachments=[
            _attachment(
                attachment_id="forward-1",
                filename="trace.json",
                url="https://example.invalid/trace.json",
                proxy_url="https://cdn.example.invalid/trace.json",
                content_type="application/json",
                size=64,
            )
        ],
    )
    msg = _message(
        author=_user("5"),
        guild_id="g1",
        channel_id="c1",
        message_snapshots=(snapshot,),
    )

    assert normalized_message_content(msg) == "Forwarded content"

    metadata = build_message_metadata(msg)
    assert metadata["forwarded_messages"] == [
        {
            "content": "Forwarded content",
            "created_at": "2026-07-07T00:00:00+00:00",
            "type": "default",
            "attachments": [
                {
                    "id": "forward-1",
                    "filename": "trace.json",
                    "url": "https://example.invalid/trace.json",
                    "proxy_url": "https://cdn.example.invalid/trace.json",
                    "content_type": "application/json",
                    "size": 64,
                    "description": None,
                    "spoiler": False,
                }
            ],
        }
    ]
