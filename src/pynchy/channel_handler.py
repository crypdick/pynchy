"""Channel communication — broadcasting messages, reactions, typing, and host messages.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from pynchy.db import store_message_direct
from pynchy.event_bus import MessageEvent
from pynchy.logger import logger
from pynchy.utils import generate_message_id

if TYPE_CHECKING:
    from pynchy.event_bus import EventBus
    from pynchy.types import Channel


class ChannelDeps(Protocol):
    """Dependencies for channel communication."""

    @property
    def channels(self) -> list[Channel]: ...

    @property
    def event_bus(self) -> EventBus: ...


async def broadcast_to_channels(
    deps: ChannelDeps, chat_jid: str, text: str, *, suppress_errors: bool = True
) -> None:
    """Send a message to all connected channels.

    Args:
        deps: Channel dependencies
        chat_jid: Target chat JID
        text: Message text to send
        suppress_errors: If True, silently ignore channel send failures
    """
    caught: tuple[type[BaseException], ...] = (
        (OSError, TimeoutError, ConnectionError) if suppress_errors else (Exception,)
    )
    for ch in deps.channels:
        if ch.is_connected():
            try:
                await ch.send_message(chat_jid, text)
            except caught as exc:
                logger.warning("Channel send failed", channel=ch.name, err=str(exc))


async def send_reaction_to_channels(
    deps: ChannelDeps, chat_jid: str, message_id: str, sender: str, emoji: str
) -> None:
    """Send a reaction emoji to a message on all channels that support it."""
    for ch in deps.channels:
        if ch.is_connected() and hasattr(ch, "send_reaction"):
            try:
                await ch.send_reaction(chat_jid, message_id, sender, emoji)
            except (OSError, TimeoutError, ConnectionError) as exc:
                logger.debug("Reaction send failed", channel=ch.name, err=str(exc))


async def set_typing_on_channels(deps: ChannelDeps, chat_jid: str, is_typing: bool) -> None:
    """Set typing indicator on all channels that support it."""
    for ch in deps.channels:
        if ch.is_connected() and hasattr(ch, "set_typing"):
            try:
                await ch.set_typing(chat_jid, is_typing)
            except (OSError, TimeoutError, ConnectionError) as exc:
                logger.debug("Typing indicator send failed", channel=ch.name, err=str(exc))


async def broadcast_host_message(deps: ChannelDeps, chat_jid: str, text: str) -> None:
    """Send operational notification from the host/platform to the user.

    Host messages are purely operational notifications (errors, status updates,
    confirmations) that are OUTSIDE the LLM's conversation. They are:
    - Sent to the user via active channels
    - Stored in message history for user reference
    - NOT sent to the LLM as system messages or user messages
    - NOT part of the SDK conversation flow

    This is distinct from SDK system messages, which provide context TO the LLM.

    Examples: "⚠️ Agent error occurred", "Context cleared", deployment notifications.
    """
    ts = datetime.now(UTC).isoformat()
    await store_message_direct(
        id=generate_message_id("host"),
        chat_jid=chat_jid,
        sender="host",
        sender_name="host",
        content=text,
        timestamp=ts,
        is_from_me=True,
        message_type="host",
    )
    channel_text = f"\U0001f3e0 {text}"
    await broadcast_to_channels(deps, chat_jid, channel_text)
    deps.event_bus.emit(
        MessageEvent(
            chat_jid=chat_jid,
            sender_name="host",
            content=text,
            timestamp=ts,
            is_bot=True,
        )
    )
