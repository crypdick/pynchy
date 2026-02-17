"""Unified message bus — single broadcast path for ALL outbound channel messages.

Every outbound message to channels routes through this module. This replaces
the 4+ scattered broadcast loops that previously lived in channel_handler,
session_handler, output_handler, and message_handler.

The IPC stdin path (message_handler.py formatting ``sender_name: content`` for
the container) is intentionally separate — it formats messages for the Claude
SDK conversation, not for human-facing channels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.types import Channel


class BusDeps(Protocol):
    """Minimal dependencies for the message bus."""

    @property
    def channels(self) -> list[Channel]: ...

    def get_channel_jid(self, canonical_jid: str, channel_name: str) -> str | None: ...


async def broadcast(
    deps: BusDeps,
    chat_jid: str,
    text: str,
    *,
    suppress_errors: bool = True,
    skip_channel: str | None = None,
) -> None:
    """Send a message to all connected channels.

    This is the single broadcast path for all outbound messages. Callers
    format their text before calling — the bus handles channel iteration,
    JID aliasing, error handling, and optional source-channel skipping.

    Args:
        deps: Provides ``channels`` and ``get_channel_jid``.
        chat_jid: Canonical chat JID (the one in registered_groups).
        text: Pre-formatted message text to send.
        suppress_errors: If True, catch network errors silently. If False,
            catch all Exceptions (log but don't raise).
        skip_channel: If set, skip the channel with this name (used for
            cross-channel echo to avoid sending back to the source).
    """
    caught: tuple[type[BaseException], ...] = (
        (OSError, TimeoutError, ConnectionError) if suppress_errors else (Exception,)
    )
    for ch in deps.channels:
        if not ch.is_connected():
            continue
        if skip_channel and ch.name == skip_channel:
            continue
        target_jid = deps.get_channel_jid(chat_jid, ch.name) or chat_jid
        try:
            await ch.send_message(target_jid, text)
        except caught as exc:
            logger.warning("Channel send failed", channel=ch.name, err=str(exc))


async def finalize_stream_or_broadcast(
    deps: BusDeps,
    chat_jid: str,
    text: str,
    stream_message_ids: dict[str, str] | None,
    *,
    suppress_errors: bool = True,
) -> None:
    """Finalize streaming messages or fall back to normal broadcast.

    For channels that were actively streaming (have a message_id in
    ``stream_message_ids``), update the existing message in-place with
    final text. For all other connected channels, send a new message.

    Args:
        deps: Provides ``channels`` and ``get_channel_jid``.
        chat_jid: Canonical chat JID.
        text: Final formatted text.
        stream_message_ids: Mapping of channel_name → message_id from
            streaming. Pass None or empty dict to broadcast normally.
        suppress_errors: Error handling mode (same as ``broadcast``).
    """
    if not stream_message_ids:
        await broadcast(deps, chat_jid, text, suppress_errors=suppress_errors)
        return

    for ch in deps.channels:
        ch_name = getattr(ch, "name", "?")
        msg_id = stream_message_ids.get(ch_name)

        if msg_id and hasattr(ch, "update_message"):
            try:
                await ch.update_message(chat_jid, msg_id, text)
            except Exception as exc:
                logger.debug("Final stream update failed", channel=ch_name, err=str(exc))
        elif ch.is_connected():
            target_jid = deps.get_channel_jid(chat_jid, ch.name) or chat_jid
            try:
                await ch.send_message(target_jid, text)
            except Exception as exc:
                logger.warning("Channel send failed", channel=ch_name, err=str(exc))
