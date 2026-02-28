"""Channel communication — broadcasting messages, reactions, and typing.

Extracted from app.py to keep the orchestrator focused on wiring.
All broadcast logic delegates to ``messaging.bus`` — the single code path
for channel iteration, JID resolution, and error handling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pynchy.host.orchestrator.messaging.sender import resolve_target_jid
from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.types import Channel


class ChannelDeps(Protocol):
    """Dependencies for channel communication."""

    @property
    def channels(self) -> list[Channel]: ...


async def send_reaction_to_channels(
    deps: ChannelDeps, chat_jid: str, message_id: str, sender: str, emoji: str
) -> None:
    """Send a reaction emoji to a message on all channels that support it."""
    for ch in deps.channels:
        if ch.is_connected() and hasattr(ch, "send_reaction"):
            target_jid = resolve_target_jid(chat_jid, ch)
            if not target_jid:
                continue
            try:
                await ch.send_reaction(target_jid, message_id, sender, emoji)
            except (OSError, TimeoutError, ConnectionError) as exc:
                logger.debug("Reaction send failed", channel=ch.name, err=str(exc))


async def send_reaction_to_outbound(
    deps: ChannelDeps,
    chat_jid: str,
    per_channel_ids: dict[str, str],
    emoji: str,
) -> None:
    """Send a reaction to an outbound message using per-channel message IDs.

    Unlike ``send_reaction_to_channels`` (which takes a single canonical
    inbound message ID), this accepts a mapping of channel_name -> raw_ts
    from the streaming pipeline.  Each channel's raw ts is wrapped as
    ``slack-{ts}`` so ``send_reaction`` can extract it.
    """
    for ch in deps.channels:
        ch_name = getattr(ch, "name", "?")
        raw_ts = per_channel_ids.get(ch_name)
        if not raw_ts:
            continue
        if not ch.is_connected() or not hasattr(ch, "send_reaction"):
            continue
        target_jid = resolve_target_jid(chat_jid, ch)
        if not target_jid:
            continue
        try:
            await ch.send_reaction(target_jid, f"slack-{raw_ts}", "", emoji)
        except (OSError, TimeoutError, ConnectionError) as exc:
            logger.debug("Outbound reaction send failed", channel=ch_name, err=str(exc))


async def set_typing_on_channels(deps: ChannelDeps, chat_jid: str, is_typing: bool) -> None:
    """Set typing indicator on all channels that support it."""
    for ch in deps.channels:
        if ch.is_connected() and hasattr(ch, "set_typing"):
            target_jid = resolve_target_jid(chat_jid, ch)
            if not target_jid:
                continue
            try:
                await ch.set_typing(target_jid, is_typing)
            except (OSError, TimeoutError, ConnectionError) as exc:
                logger.debug("Typing indicator send failed", channel=ch.name, err=str(exc))
