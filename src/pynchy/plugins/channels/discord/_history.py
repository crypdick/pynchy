"""Discord inbound-history conversion helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import discord

from pynchy.plugins.api import NewMessage

from ._events import build_message_metadata, normalized_message_content
from ._models import DiscordInboundMessage

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
    message: DiscordInboundMessage,
    bot_user_id: str,
) -> NewMessage | None:
    if message.author.is_bot or message.author.id == bot_user_id:
        return None
    timestamp = message.created_at.isoformat() if message.created_at else ""
    return NewMessage(
        id=f"{MESSAGE_ID_PREFIX}{message.id}",
        chat_jid=channel_jid,
        sender=message.author.id,
        sender_name=message.author.display_name or message.author.rendered_name,
        content=normalized_message_content(message),
        timestamp=timestamp or datetime.now(UTC).isoformat(),
        is_from_me=False,
        metadata=build_message_metadata(message),
    )
