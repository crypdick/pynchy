"""Channel communication — broadcasting messages, reactions, and typing.

Extracted from app.py to keep the orchestrator focused on wiring.
All broadcast logic delegates to ``messaging.bus`` — the single code path
for channel iteration, JID resolution, and error handling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.types import Channel


class ChannelDeps(Protocol):
    """Dependencies for channel communication."""

    @property
    def channels(self) -> list[Channel]: ...

    def get_channel_jid(self, canonical_jid: str, channel_name: str) -> str | None: ...


async def send_reaction_to_channels(
    deps: ChannelDeps, chat_jid: str, message_id: str, sender: str, emoji: str
) -> None:
    """Send a reaction emoji to a message on all channels that support it."""
    for ch in deps.channels:
        if ch.is_connected() and hasattr(ch, "send_reaction"):
            target_jid = deps.get_channel_jid(chat_jid, ch.name) or chat_jid
            try:
                await ch.send_reaction(target_jid, message_id, sender, emoji)
            except (OSError, TimeoutError, ConnectionError) as exc:
                logger.debug("Reaction send failed", channel=ch.name, err=str(exc))


async def set_typing_on_channels(deps: ChannelDeps, chat_jid: str, is_typing: bool) -> None:
    """Set typing indicator on all channels that support it."""
    for ch in deps.channels:
        if ch.is_connected() and hasattr(ch, "set_typing"):
            target_jid = deps.get_channel_jid(chat_jid, ch.name) or chat_jid
            try:
                await ch.set_typing(target_jid, is_typing)
            except (OSError, TimeoutError, ConnectionError) as exc:
                logger.debug("Typing indicator send failed", channel=ch.name, err=str(exc))
