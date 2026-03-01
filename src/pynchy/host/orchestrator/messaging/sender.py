"""Unified message bus — single broadcast path for ALL outbound channel messages.

Every outbound message to channels routes through this module. This replaces
the 4+ scattered broadcast loops that previously lived in channel_handler,
session_handler, output_handler, and message_handler.

The IPC stdin path (message_handler.py formatting ``sender_name: content`` for
the container) is intentionally separate — it formats messages for the Claude
SDK conversation, not for human-facing channels.

Outbound messages are recorded in the ledger (best-effort) so the reconciler
can retry failed deliveries.  If the ledger write itself fails, delivery
proceeds fire-and-forget — the same behaviour as before the ledger existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.types import Channel, OutboundEvent


class BusDeps(Protocol):
    """Minimal dependencies for the message bus."""

    @property
    def channels(self) -> list[Channel]: ...

    @property
    def workspaces(self) -> dict: ...


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
        from pynchy.state import record_outbound

        return await record_outbound(chat_jid, text, source, channel_names)
    except Exception:
        logger.debug("Outbound ledger write failed (fire-and-forget fallback)")
        return None


async def _mark_success(ledger_id: int | None, channel_name: str) -> None:
    if ledger_id is None:
        return
    try:
        from pynchy.state import mark_delivered

        await mark_delivered(ledger_id, channel_name)
    except Exception:
        logger.debug("Ledger mark_delivered failed (best-effort)", channel=channel_name)


async def _mark_error(ledger_id: int | None, channel_name: str, error: str) -> None:
    if ledger_id is None:
        return
    try:
        from pynchy.state import mark_delivery_error

        await mark_delivery_error(ledger_id, channel_name, error)
    except Exception:
        logger.debug("Ledger mark_delivery_error failed (best-effort)", channel=channel_name)


# ---------------------------------------------------------------------------
# Access check helpers
# ---------------------------------------------------------------------------


def _channel_allows_outbound(deps: BusDeps, chat_jid: str, channel_name: str) -> bool:
    """Check if a channel's resolved access mode permits outbound messages.

    Returns True if outbound is allowed (access is "write" or "readwrite").
    Returns True if no workspace is found (default to allowing).
    """
    group = _find_workspace_by_jid(deps, chat_jid)
    if group is None:
        return True
    from pynchy.config.access import resolve_channel_config, resolve_workspace_connection_name

    expected = resolve_workspace_connection_name(group.folder)
    if expected and expected != channel_name:
        return False

    resolved = resolve_channel_config(
        group.folder,
        channel_jid=chat_jid,
        channel_plugin_name=channel_name,
    )
    return resolved.access != "read"


def _find_workspace_by_jid(deps: BusDeps, chat_jid: str) -> object | None:
    """Find workspace profile by canonical JID."""
    workspaces = deps.workspaces
    if not workspaces:
        return None
    return workspaces.get(chat_jid)


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

    Returns ``(channel, target_jid)`` pairs for channels that are connected,
    allowed outbound by access rules, and own the JID.
    """
    targets: list[tuple[Channel, str]] = []
    for ch in deps.channels:
        if not ch.is_connected():
            continue
        if skip_channel and ch.name == skip_channel:
            continue
        if not _channel_allows_outbound(deps, chat_jid, ch.name):
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


async def broadcast(
    deps: BusDeps,
    chat_jid: str,
    event: OutboundEvent,
    *,
    suppress_errors: bool = True,
    skip_channel: str | None = None,
    source: str = "broadcast",
) -> None:
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
    """
    caught: tuple[type[BaseException], ...] = (
        (OSError, TimeoutError, ConnectionError) if suppress_errors else (Exception,)
    )

    targets = _resolve_send_targets(deps, chat_jid, skip_channel=skip_channel)

    # Record to outbound ledger (best-effort) — store the text content
    ledger_id = await _record_to_ledger(
        chat_jid, event.content, source, [ch.name for ch, _ in targets]
    )

    # Deliver to each target
    for ch, target_jid in targets:
        try:
            await ch.send_event(target_jid, event)
            await _mark_success(ledger_id, ch.name)
        except caught as exc:
            logger.warning("Channel send failed", channel=ch.name, err=str(exc))
            await _mark_error(ledger_id, ch.name, str(exc))


async def finalize_stream_or_broadcast(
    deps: BusDeps,
    chat_jid: str,
    event: OutboundEvent,
    stream_message_ids: dict[str, str] | None,
    *,
    suppress_errors: bool = True,
) -> None:
    """Finalize streaming messages or fall back to normal broadcast.

    For channels that were actively streaming (have a message_id in
    ``stream_message_ids``), update the existing message in-place with
    the final event. For all other connected channels, send a new message.

    Args:
        deps: Provides ``channels`` and ``workspaces``.
        chat_jid: Canonical chat JID.
        event: Structured outbound event with final content.
        stream_message_ids: Mapping of channel_name -> message_id from
            streaming. Pass None or empty dict to broadcast normally.
        suppress_errors: Error handling mode (same as ``broadcast``).
    """
    if not stream_message_ids:
        await broadcast(deps, chat_jid, event, suppress_errors=suppress_errors, source="agent")
        return

    # Match broadcast()'s error handling: suppress_errors=True catches only
    # network errors (letting programming bugs propagate); False catches all.
    caught: tuple[type[BaseException], ...] = (
        (OSError, TimeoutError, ConnectionError) if suppress_errors else (Exception,)
    )

    # Resolve non-streaming targets via the shared helper
    send_targets = _resolve_send_targets(deps, chat_jid)
    send_target_names = {ch.name for ch, _ in send_targets}

    # Identify streaming targets (channels with a message_id and update_event).
    stream_targets: list[tuple[Channel, str, str]] = []  # (ch, msg_id, target_jid)
    for ch in deps.channels:
        ch_name = ch.name
        msg_id = stream_message_ids.get(ch_name)
        if not msg_id or not hasattr(ch, "update_event"):
            continue
        if not _channel_allows_outbound(deps, chat_jid, ch_name):
            continue
        target_jid = resolve_target_jid(chat_jid, ch)
        if not target_jid:
            continue
        stream_targets.append((ch, msg_id, target_jid))
    stream_target_names = {ch.name for ch, _, _ in stream_targets}

    # Remove streaming channels from send targets (they get update_event instead)
    send_targets = [(ch, jid) for ch, jid in send_targets if ch.name not in stream_target_names]

    # Record to ledger
    all_target_names = sorted(stream_target_names | send_target_names)
    ledger_id = await _record_to_ledger(chat_jid, event.content, "agent", all_target_names)

    # Deliver: update streamed messages in-place, falling back to send_event.
    # update_event failures always trigger fallback (catch Exception);
    # send_event failures respect suppress_errors via `caught`.
    for ch, msg_id, target_jid in stream_targets:
        try:
            await ch.update_event(target_jid, msg_id, event)
            await _mark_success(ledger_id, ch.name)
        except Exception:
            logger.warning("Stream update failed, falling back to send_event", channel=ch.name)
            try:
                await ch.send_event(target_jid, event)
                await _mark_success(ledger_id, ch.name)
            except caught as exc:
                logger.warning("Fallback send_event also failed", channel=ch.name, err=str(exc))
                await _mark_error(ledger_id, ch.name, str(exc))

    # Deliver: send to non-streaming channels
    for ch, target_jid in send_targets:
        try:
            await ch.send_event(target_jid, event)
            await _mark_success(ledger_id, ch.name)
        except caught as exc:
            logger.warning("Channel send failed", channel=ch.name, err=str(exc))
            await _mark_error(ledger_id, ch.name, str(exc))
