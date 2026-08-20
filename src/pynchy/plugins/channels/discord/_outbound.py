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
    target = channel
    for chunk in chunk_discord_text(text):
        target, _ = await _send_chunk(target, chunk)


async def _send_chunk(
    channel: object,
    content: str,
    *,
    view: discord.ui.View | None = None,
) -> tuple[object, object]:
    channel_like = cast("Any", channel)
    kwargs: dict[str, object] = {
        "allowed_mentions": discord.AllowedMentions.none(),
        "suppress_embeds": True,
    }
    if view is not None:
        kwargs["view"] = view

    send = getattr(channel_like, "send", None)
    if callable(send):
        return channel, await send(content, **kwargs)

    create_thread = getattr(channel_like, "create_thread", None)
    if not callable(create_thread):
        raise TypeError(f"{type(channel_like).__name__} object does not support sending")
    created = await create_thread(name="Pynchy", content=content, **kwargs)
    return getattr(created, "thread", created), getattr(created, "message", created)


async def send_approval(  # noqa: PLR0913 - approval control state stays explicit.
    channel: object,
    owner: DiscordChannel,
    jid: str,
    text: str,
    short_id: str,
    *,
    allow_remember: bool = False,
) -> None:
    """Send an approval prompt with controls on the first Discord chunk."""
    chunks = chunk_discord_text(text)
    view = DiscordApprovalView(
        channel=owner,
        jid=jid,
        short_id=short_id,
        content=chunks[0],
        allow_remember=allow_remember,
    )
    target, message = await _send_chunk(channel, chunks[0], view=view)
    view.bind_message_id(str(message.id))
    for chunk in chunks[1:]:
        target, _ = await _send_chunk(target, chunk)
