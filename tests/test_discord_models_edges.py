"""Boundary coverage for exported Discord message parsers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pynchy.plugins.channels.discord import parse_discord_message, parse_discord_reaction


@dataclass
class _Author:
    id: int
    display_name: str
    global_name: str
    name: str
    bot: bool = False
    roles: tuple[object, ...] = ()

    def __str__(self) -> str:
        return self.name


@dataclass
class _Role:
    id: int


@dataclass
class _Channel:
    id: int
    name: str | None = None
    parent: object | None = None
    parent_id: int | None = None


@dataclass
class _Attachment:
    id: int
    filename: str
    url: str
    proxy_url: str
    content_type: str | None
    size: int
    description: str | None
    spoiler: bool
    read: object | None = None


@dataclass
class _Reference:
    message_id: int | None
    resolved: object | None


@dataclass
class _Snapshot:
    content: str
    created_at: datetime
    type: str
    attachments: tuple[object, ...]


@dataclass
class _Message:
    id: int
    author: _Author
    channel: _Channel
    guild: object | None
    created_at: datetime
    type: object
    content: str
    attachments: tuple[object, ...]
    reference: object | None
    message_snapshots: tuple[object, ...]
    mentions: tuple[object, ...]


def test_parse_discord_message_preserves_nested_metadata():
    created_at = datetime(2026, 7, 29, tzinfo=UTC)
    parent = _Channel(20, "parent")
    author = _Author(42, "Alice", "alice-global", "alice", roles=(_Role(7),))
    attachment = _Attachment(
        8,
        "photo.png",
        "https://example/photo",
        "https://example/proxy",
        "image/png",
        123,
        "caption",
        True,
        read=lambda: b"data",
    )
    resolved = _Message(
        id=91,
        author=_Author(99, "Bob", "bob-global", "bob"),
        channel=_Channel(20, "parent"),
        guild=None,
        created_at=created_at,
        type="reply",
        content="original",
        attachments=(),
        reference=None,
        message_snapshots=(),
        mentions=(),
    )
    message = _Message(
        id=90,
        author=author,
        channel=_Channel(21, "thread", parent=parent, parent_id=20),
        guild=type("Guild", (), {"id": 1, "name": "Pynchy"})(),
        created_at=created_at,
        type=type("MessageType", (), {"name": "default"})(),
        content="hello",
        attachments=(attachment,),
        reference=_Reference(91, resolved),
        message_snapshots=(_Snapshot("forwarded", created_at, "forward", (attachment,)),),
        mentions=(_Author(100, "Bot", "bot", "bot"),),
    )

    parsed = parse_discord_message(message)

    assert parsed.id == "90"
    assert parsed.author.role_ids == frozenset({"7"})
    assert parsed.channel.parent_id == "20"
    assert parsed.attachments[0].read is not None
    assert parsed.reply is not None
    assert parsed.reply.sender_name == "Bob"
    assert parsed.reply.content == "original"
    assert parsed.forwarded_messages[0].type_name == "forward"
    assert parsed.mentioned_user_ids == frozenset({"100"})
    assert parsed.system_type == "default"


def test_parse_discord_message_handles_unresolved_reply_and_non_reader_attachment():
    resolved_without_author = type("Resolved", (), {"content": "system reply"})()
    message = _Message(
        id=90,
        author=_Author(42, "Alice", "alice", "alice"),
        channel=_Channel(21),
        guild=None,
        created_at=None,  # type: ignore[arg-type]
        type=None,
        content="hello",
        attachments=(_Attachment(8, "file", "url", "proxy", None, 0, None, False, read=1),),
        reference=_Reference(91, resolved_without_author),
        message_snapshots=(),
        mentions=(),
    )

    parsed = parse_discord_message(message)

    assert parsed.reply is not None
    assert parsed.reply.message_id == "91"
    assert parsed.reply.sender_name is None
    assert parsed.reply.content == "system reply"
    assert parsed.attachments[0].read is None
    assert parsed.created_at is None
    assert parsed.system_type is None

    message.reference = _Reference(None, None)
    unresolved = parse_discord_message(message)
    assert unresolved.reply is not None
    assert unresolved.reply.message_id is None
    assert not unresolved.reply.content


def test_parse_discord_reaction_supports_dm_and_guild_payloads():
    dm = type(
        "Reaction",
        (),
        {"user_id": 42, "guild_id": None, "channel_id": 7, "message_id": 8, "emoji": "👍"},
    )()
    guild = type(
        "Reaction",
        (),
        {"user_id": 43, "guild_id": 1, "channel_id": 9, "message_id": 10, "emoji": "✅"},
    )()

    assert parse_discord_reaction(dm).guild_id is None
    assert parse_discord_reaction(guild).guild_id == "1"
