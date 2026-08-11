"""Unified message bus for ordinary outbound channel messages.

Consecutive editable messages use the capability-driven collaborator in
``updating.py``; both paths share the same outbound ledger semantics.

The IPC stdin path (message_handler.py formatting ``sender_name: content`` for
the container) is intentionally separate — it formats messages for the Claude
SDK conversation, not for human-facing channels.

Outbound messages are recorded in the ledger (best-effort) so the reconciler
can retry failed deliveries.  If the ledger write itself fails, delivery
proceeds fire-and-forget — the same behaviour as before the ledger existed.
"""

from __future__ import annotations

from asyncio import Lock
from typing import Protocol, runtime_checkable
from weakref import WeakValueDictionary

from pynchy.identifiers import (
    ChannelName,
    ChatJid,
)
from pynchy.logger import logger
from pynchy.plugins.api import (  # noqa: TC001 - beartype resolves contract annotations at runtime.
    Channel,
    OutboundEvent,
)
from pynchy.state import api as state
from pynchy.state.api import OutboundDelivery, OutboundDeliveryOperation


@runtime_checkable
class BusDeps(Protocol):
    """Minimal dependencies for the message bus."""

    @property
    def channels(self) -> list[Channel]: ...


_outbound_delivery_locks: WeakValueDictionary[str, Lock] = WeakValueDictionary()


def outbound_delivery_lock(chat_jid: str) -> Lock:
    """Serialize ledger-backed provider sends for one chat in this host process."""
    lock = _outbound_delivery_locks.get(chat_jid)
    if lock is None:
        lock = Lock()
        _outbound_delivery_locks[chat_jid] = lock
    return lock


# ---------------------------------------------------------------------------
# Ledger helpers (best-effort — failures never block delivery)
# ---------------------------------------------------------------------------


async def _record_to_ledger(
    chat_jid: str, text: str, source: str, channel_names: list[str]
) -> int | None:
    """Record an outbound message to the ledger.

    Returns the ledger_id on success, None on failure.
    """
    if not channel_names:
        return None
    try:
        return await state.record_outbound(
            ChatJid(chat_jid), text, source, [ChannelName(c) for c in channel_names]
        )
    except Exception:  # noqa: BLE001 - outbound ledger write is best-effort and must not block delivery.
        logger.debug("Outbound ledger write failed (fire-and-forget fallback)")
        return None


async def _record_delivery_mutations(
    chat_jid: str,
    text: str,
    source: str,
    deliveries: list[OutboundDelivery],
) -> int | None:
    """Record retry semantics for explicit provider mutations."""
    if not deliveries:
        return None
    try:
        return await state.record_outbound_deliveries(
            ChatJid(chat_jid),
            text,
            source,
            deliveries,
        )
    except Exception:  # noqa: BLE001 - outbound ledger write is best-effort and must not block delivery.
        logger.debug("Outbound ledger write failed (fire-and-forget fallback)")
        return None


async def _mark_success(ledger_id: int | None, channel_name: str) -> None:
    if ledger_id is None:
        return
    try:
        await state.mark_delivered(ledger_id, channel_name)
    except Exception:  # noqa: BLE001 - ledger success marking is best-effort bookkeeping.
        logger.debug("Ledger mark_delivered failed (best-effort)", channel=channel_name)


async def _mark_error(ledger_id: int | None, channel_name: str, error: str) -> None:
    if ledger_id is None:
        return
    try:
        await state.mark_delivery_error(ledger_id, channel_name, error)
    except Exception:  # noqa: BLE001 - ledger error marking is best-effort bookkeeping.
        logger.debug("Ledger mark_delivery_error failed (best-effort)", channel=channel_name)


# ---------------------------------------------------------------------------
# Target resolution — single implementation for channel filtering
# ---------------------------------------------------------------------------


def _resolve_send_targets(
    deps: BusDeps,
    chat_jid: str,
    *,
    skip_channel: str | None = None,
) -> list[tuple[Channel, str]]:
    """Resolve which channels should receive an outbound event.

    Returns ``(channel, target_jid)`` pairs for channels that own the JID.
    """
    targets: list[tuple[Channel, str]] = []
    for ch in deps.channels:
        if skip_channel and ch.name == skip_channel:
            continue
        target_jid = resolve_target_jid(chat_jid, ch)
        if not target_jid:
            continue
        targets.append((ch, target_jid))
    return targets


def resolve_target_jid(chat_jid: str, channel: Channel) -> str | None:
    """Return *chat_jid* if the channel owns it, otherwise None.

    Public within the chat package — used by bus, channel_handler, and streaming.
    """
    if channel.owns_jid(chat_jid):
        return chat_jid
    return None


# ---------------------------------------------------------------------------
# Broadcast functions
# ---------------------------------------------------------------------------


async def broadcast(  # noqa: PLR0913 - outbound bus keeps the full routing/broadcast contract explicit.
    deps: BusDeps,
    chat_jid: str,
    event: OutboundEvent,
    *,
    suppress_errors: bool = True,
    skip_channel: str | None = None,
    source: str = "broadcast",
) -> bool:
    """Send an event to all connected channels.

    This is the single broadcast path for all outbound messages. Callers
    construct an ``OutboundEvent`` before calling — the bus handles channel
    iteration, error handling, and optional source-channel skipping.

    Args:
        deps: Provides ``channels`` and ``workspaces``.
        chat_jid: Canonical chat JID (the one in registered_groups).
        event: Structured outbound event to send.
        suppress_errors: If True, catch network errors silently. If False,
            catch all Exceptions (log but don't raise).
        skip_channel: If set, skip the channel with this name (used for
            cross-channel echo to avoid sending back to the source).
        source: Ledger source label (e.g. ``"broadcast"``, ``"cross_post"``).

    Returns:
        True when at least one channel accepted the event.
    """
    caught: tuple[type[BaseException], ...] = (
        (OSError, TimeoutError, ConnectionError) if suppress_errors else (Exception,)
    )

    owned_targets = _resolve_send_targets(
        deps,
        chat_jid,
        skip_channel=skip_channel,
    )
    targets = [(ch, target_jid) for ch, target_jid in owned_targets if ch.is_connected()]

    async with outbound_delivery_lock(chat_jid):
        # Record to outbound ledger (best-effort) — store the text content
        ledger_id = await _record_to_ledger(
            chat_jid, event.content, source, [ch.name for ch, _ in owned_targets]
        )

        delivered = False
        for ch, target_jid in targets:
            try:
                await ch.send_event(target_jid, event)
                await _mark_success(ledger_id, ch.name)
                delivered = True
            except caught as exc:
                logger.warning("Channel send failed", channel=ch.name, err=str(exc))
                await _mark_error(ledger_id, ch.name, str(exc))
    return delivered


async def finalize_stream_or_broadcast(
    deps: BusDeps,
    chat_jid: str,
    event: OutboundEvent,
    stream_message_ids: dict[str, str] | None,
    *,
    suppress_errors: bool = True,
) -> bool:
    """Finalize streaming messages or fall back to normal broadcast.

    For channels that were actively streaming (have a message_id in
    ``stream_message_ids``), update the existing message in-place with
    the final event. For all other connected channels, send a separate message.

    Args:
        deps: Provides ``channels`` and ``workspaces``.
        chat_jid: Canonical chat JID.
        event: Structured outbound event with final content.
        stream_message_ids: Mapping of channel_name -> message_id from
            streaming. Pass None or empty dict to broadcast normally.
        suppress_errors: Error handling mode (same as ``broadcast``).

    Returns:
        True when at least one stream update or channel send succeeded.
    """
    if not stream_message_ids:
        return await broadcast(
            deps,
            chat_jid,
            event,
            suppress_errors=suppress_errors,
            source="agent",
        )

    caught = _caught_errors(suppress_errors=suppress_errors)
    owned_send_targets = _resolve_send_targets(deps, chat_jid)
    stream_targets = _resolve_stream_targets(deps, chat_jid, stream_message_ids)
    stream_target_names = {ch.name for ch, _, _ in stream_targets}
    owned_send_targets = [
        (ch, jid) for ch, jid in owned_send_targets if ch.name not in stream_target_names
    ]
    send_targets = [(ch, jid) for ch, jid in owned_send_targets if ch.is_connected()]

    async with outbound_delivery_lock(chat_jid):
        ledger_id = await _record_delivery_mutations(
            chat_jid,
            event.content,
            "agent",
            [
                *[
                    OutboundDelivery(
                        channel_name=ChannelName(ch.name),
                        operation=OutboundDeliveryOperation.EDIT,
                        remote_message_id=message_id,
                    )
                    for ch, message_id, _ in stream_targets
                ],
                *[
                    OutboundDelivery(channel_name=ChannelName(ch.name))
                    for ch, _ in owned_send_targets
                ],
            ],
        )
        updated = await _deliver_stream_targets(stream_targets, event, ledger_id, caught)
        sent = await _deliver_send_targets(send_targets, event, ledger_id, caught)
    return updated or sent


def _caught_errors(*, suppress_errors: bool) -> tuple[type[BaseException], ...]:
    # Match broadcast()'s error handling: suppress_errors=True catches only
    # network errors (letting programming bugs propagate); False catches all.
    return (OSError, TimeoutError, ConnectionError) if suppress_errors else (Exception,)


def _resolve_stream_targets(
    deps: BusDeps,
    chat_jid: str,
    stream_message_ids: dict[str, str],
) -> list[tuple[Channel, str, str]]:
    stream_targets: list[tuple[Channel, str, str]] = []
    for ch in deps.channels:
        ch_name = ch.name
        msg_id = stream_message_ids.get(ch_name)
        if not msg_id or not hasattr(ch, "update_event"):
            continue
        target_jid = resolve_target_jid(chat_jid, ch)
        if not target_jid:
            continue
        stream_targets.append((ch, msg_id, target_jid))
    return stream_targets


async def _deliver_stream_targets(
    stream_targets: list[tuple[Channel, str, str]],
    event: OutboundEvent,
    ledger_id: int | None,
    caught: tuple[type[BaseException], ...],
) -> bool:
    # update_event failures always fall back to send_event (catch Exception);
    # send_event failures respect suppress_errors via `caught`.
    delivered = False
    for ch, msg_id, target_jid in stream_targets:
        try:
            await ch.update_event(target_jid, msg_id, event)
            await _mark_success(ledger_id, ch.name)
            delivered = True
        except Exception:  # noqa: BLE001 - stream update retry keeps delivery moving.
            logger.warning("Stream update failed, falling back to send_event", channel=ch.name)
            try:
                await ch.send_event(target_jid, event)
                await _mark_success(ledger_id, ch.name)
                delivered = True
            except caught as exc:
                logger.warning("Fallback send_event also failed", channel=ch.name, err=str(exc))
                await _mark_error(ledger_id, ch.name, str(exc))
    return delivered


async def _deliver_send_targets(
    send_targets: list[tuple[Channel, str]],
    event: OutboundEvent,
    ledger_id: int | None,
    caught: tuple[type[BaseException], ...],
) -> bool:
    delivered = False
    for ch, target_jid in send_targets:
        try:
            await ch.send_event(target_jid, event)
            await _mark_success(ledger_id, ch.name)
            delivered = True
        except caught as exc:
            logger.warning("Channel send failed", channel=ch.name, err=str(exc))
            await _mark_error(ledger_id, ch.name, str(exc))
    return delivered
