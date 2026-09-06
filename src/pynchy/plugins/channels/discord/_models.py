"""Pynchy-owned views of discord.py gateway payloads.

discord.py exposes large, mutable SDK objects. Parse them at the adapter
boundary so routing and message construction use the small typed shape Pynchy
actually consumes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class DiscordAuthor:
    """Identity and access fields extracted from a Discord user."""

    id: str
    is_bot: bool
    role_ids: frozenset[str]
    display_name: str | None
    global_name: str | None
    name: str | None
    rendered_name: str


@dataclass(frozen=True, slots=True)
class DiscordChannelDetails:
    """Channel identity and optional thread-parent information."""

    id: str
    name: str | None
    parent_id: str | None
    parent_name: str | None


@dataclass(frozen=True, slots=True)
class DiscordAttachment:
    """Attachment data Pynchy persists or sends to audio transcription."""

    id: str
    filename: str
    url: str
    proxy_url: str
    content_type: str | None
    size: int | None
    description: str | None
    spoiler: bool
    read: Callable[[], bytes | Awaitable[bytes]] | None = None


@dataclass(frozen=True, slots=True)
class DiscordReply:
    """Resolved reply data, when Discord includes it with an inbound message."""

    message_id: str | None
    sender_name: str | None
    content: str


@dataclass(frozen=True, slots=True)
class DiscordForwardedMessage:
    """A message snapshot included in a forwarded Discord message."""

    content: str
    created_at: datetime | None
    type_name: str
    attachments: tuple[DiscordAttachment, ...]


@dataclass(frozen=True, slots=True)
class DiscordInboundMessage:
    """The complete Pynchy-owned representation of one inbound Discord message."""

    id: str
    author: DiscordAuthor
    guild_id: str | None
    guild_name: str | None
    channel: DiscordChannelDetails
    content: str
    attachments: tuple[DiscordAttachment, ...]
    reply: DiscordReply | None
    forwarded_messages: tuple[DiscordForwardedMessage, ...]
    mentioned_user_ids: frozenset[str]
    system_type: str | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class DiscordInboundReaction:
    """The Pynchy-owned representation of a Discord raw-reaction event."""

    user_id: str
    guild_id: str | None
    channel_id: str
    message_id: str
    emoji: str


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_author(raw_author: object) -> DiscordAuthor:
    author = cast("Any", raw_author)
    return DiscordAuthor(
        id=str(author.id),
        is_bot=bool(getattr(author, "bot", False)),
        role_ids=frozenset(str(role.id) for role in getattr(author, "roles", [])),
        display_name=_optional_str(getattr(author, "display_name", None)),
        global_name=_optional_str(getattr(author, "global_name", None)),
        name=_optional_str(getattr(author, "name", None)),
        rendered_name=str(author),
    )


def _parse_attachment(raw_attachment: object) -> DiscordAttachment:
    attachment = cast("Any", raw_attachment)
    reader = getattr(attachment, "read", None)
    return DiscordAttachment(
        id=str(getattr(attachment, "id", "")),
        filename=str(getattr(attachment, "filename", "")),
        url=str(getattr(attachment, "url", "")),
        proxy_url=str(getattr(attachment, "proxy_url", "")),
        content_type=_optional_str(getattr(attachment, "content_type", None)),
        size=getattr(attachment, "size", None),
        description=_optional_str(getattr(attachment, "description", None)),
        spoiler=bool(getattr(attachment, "spoiler", False)),
        read=reader if callable(reader) else None,
    )


def _parse_reply(raw_reference: object) -> DiscordReply | None:
    if raw_reference is None:
        return None
    reference = cast("Any", raw_reference)
    resolved = getattr(reference, "resolved", None)
    sender_name: str | None = None
    content = ""
    if resolved is not None:
        resolved_like = cast("Any", resolved)
        author = getattr(resolved_like, "author", None)
        if author is not None:
            sender_name = _optional_str(getattr(author, "display_name", None)) or str(author)
        content = str(getattr(resolved_like, "content", ""))
    message_id = getattr(reference, "message_id", None)
    return DiscordReply(
        message_id=None if message_id is None else str(message_id),
        sender_name=sender_name,
        content=content,
    )


def _parse_forwarded_message(raw_snapshot: object) -> DiscordForwardedMessage:
    snapshot = cast("Any", raw_snapshot)
    created_at = getattr(snapshot, "created_at", None)
    return DiscordForwardedMessage(
        content=str(getattr(snapshot, "content", "")),
        created_at=created_at if isinstance(created_at, datetime) else None,
        type_name=str(getattr(snapshot, "type", "")),
        attachments=tuple(_parse_attachment(item) for item in getattr(snapshot, "attachments", [])),
    )


def parse_discord_message(raw_message: object) -> DiscordInboundMessage:
    """Parse a discord.py message at the Pynchy adapter boundary."""
    message = cast("Any", raw_message)
    guild = getattr(message, "guild", None)
    channel = message.channel
    parent = getattr(channel, "parent", None)
    created_at = getattr(message, "created_at", None)
    message_type = getattr(message, "type", None)
    parent_id = getattr(channel, "parent_id", None)
    return DiscordInboundMessage(
        id=str(message.id),
        author=_parse_author(message.author),
        guild_id=None if guild is None else str(guild.id),
        guild_name=None if guild is None else _optional_str(getattr(guild, "name", None)),
        channel=DiscordChannelDetails(
            id=str(channel.id),
            name=_optional_str(getattr(channel, "name", None)),
            parent_id=None if parent_id is None else str(parent_id),
            parent_name=None if parent is None else _optional_str(getattr(parent, "name", None)),
        ),
        content=str(getattr(message, "content", "")),
        attachments=tuple(_parse_attachment(item) for item in getattr(message, "attachments", [])),
        reply=_parse_reply(getattr(message, "reference", None)),
        forwarded_messages=tuple(
            _parse_forwarded_message(item) for item in getattr(message, "message_snapshots", [])
        ),
        mentioned_user_ids=frozenset(str(user.id) for user in getattr(message, "mentions", [])),
        system_type=_optional_str(getattr(message_type, "name", None)),
        created_at=created_at if isinstance(created_at, datetime) else None,
    )


def parse_discord_reaction(raw_payload: object) -> DiscordInboundReaction:
    """Parse a discord.py raw-reaction payload at the adapter boundary."""
    payload = cast("Any", raw_payload)
    guild_id = getattr(payload, "guild_id", None)
    return DiscordInboundReaction(
        user_id=str(payload.user_id),
        guild_id=None if guild_id is None else str(guild_id),
        channel_id=str(payload.channel_id),
        message_id=str(payload.message_id),
        emoji=str(payload.emoji),
    )
