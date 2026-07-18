"""Discord outbound-message helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import discord

from ._approval import DiscordApprovalView
from ._chunk import chunk_discord_text

if TYPE_CHECKING:
    from ._channel import DiscordChannel
else:
    DiscordChannel = object


async def send_text(channel: object, text: str) -> None:
    """Send a message in Discord-sized chunks without mentioning users."""
    channel_like = cast("Any", channel)
    for chunk in chunk_discord_text(text):
        await channel_like.send(
            chunk,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )


async def send_approval(
    channel: object,
    owner: DiscordChannel,
    jid: str,
    text: str,
    short_id: str,
) -> None:
    """Send an approval prompt with controls on the first Discord chunk."""
    chunks = chunk_discord_text(text)
    if not chunks:
        return
    view = DiscordApprovalView(
        channel=owner,
        jid=jid,
        short_id=short_id,
        content=chunks[0],
    )
    channel_like = cast("Any", channel)
    message = await channel_like.send(
        chunks[0],
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
        suppress_embeds=True,
    )
    view.bind_message_id(str(message.id))
    for chunk in chunks[1:]:
        await channel_like.send(
            chunk,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )
