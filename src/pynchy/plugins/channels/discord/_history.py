"""Discord inbound-history conversion helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import discord

from pynchy.types import NewMessage

from ._events import build_message_metadata, normalized_message_content

MESSAGE_ID_PREFIX = "discord-"


def history_after(since: str) -> discord.Object:
    return discord.Object(id=discord.utils.time_snowflake(datetime.fromisoformat(since)))


def history_high_water_mark(message: object, current: str) -> str:
    message_like = cast("Any", message)
    timestamp = message_like.created_at.isoformat() if message_like.created_at else ""
    if timestamp > current:
        return timestamp
    return current


def history_message(
    *,
    channel_jid: str,
    message: object,
    bot_user_id: str,
) -> NewMessage | None:
    message_like = cast("Any", message)
    author = message_like.author
    if getattr(author, "bot", False) or str(author.id) == bot_user_id:
        return None
    timestamp = message_like.created_at.isoformat() if message_like.created_at else ""
    return NewMessage(
        id=f"{MESSAGE_ID_PREFIX}{message_like.id}",
        chat_jid=channel_jid,
        sender=str(author.id),
        sender_name=getattr(author, "display_name", None) or str(author),
        content=normalized_message_content(message_like),
        timestamp=timestamp or datetime.now(UTC).isoformat(),
        is_from_me=False,
        metadata=build_message_metadata(message_like),
    )
