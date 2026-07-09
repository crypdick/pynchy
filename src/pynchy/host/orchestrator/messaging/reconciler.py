"""Unified channel reconciliation.

Single code path for all channels.  Per-(channel, group) cooldown prevents
excessive API calls during rapid polling cycles.  Channels that don't own
the canonical JID are skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from pynchy.logger import logger
from pynchy.state import (
    advance_cursors_atomic,
    get_channel_cursor,
    get_pending_outbound,
    mark_delivered,
    mark_delivery_error,
    message_exists,
    prune_stale_cursors,
)
from pynchy.types import (
    Channel,
    ChannelName,
    ChatJid,
    InboundFetchResult,
    NewMessage,
    OutboundEvent,
    OutboundEventType,
    WorkspaceProfile,
)

RECONCILE_COOLDOWN = timedelta(seconds=30)
_INITIAL_LOOKBACK = timedelta(hours=24)
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

# Module-level cooldown state (survives across calls within a process)
_last_reconciled: dict[tuple[str, str], datetime] = {}


@runtime_checkable
class ReconcilerDeps(Protocol):
    """Minimal dependencies for the reconciler."""

    @property
    def channels(self) -> list[Channel]: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def _ingest_user_message(
        self, msg: NewMessage, *, source_channel: str | None = None
    ) -> None: ...

    async def start_interactive_turn(self, chat_jid: str) -> None: ...


def _should_skip_pair(
    ch: Channel, canonical_jid: str, group: WorkspaceProfile | None, now: datetime
) -> bool:
    """Gate a (channel, jid) pair: connection mismatch, non-ownership, or cooldown."""
    from pynchy.config.access import resolve_workspace_connection_name

    if group is not None:
        expected = resolve_workspace_connection_name(group.folder)
        if expected and expected != ch.name:
            logger.debug(
                "connection_gate_skip",
                channel=ch.name,
                canonical_jid=canonical_jid,
                expected=expected,
            )
            return True

    if not ch.owns_jid(canonical_jid):
        logger.debug("jid_ownership_skip", channel=ch.name, canonical_jid=canonical_jid)
        return True

    key = (ch.name, canonical_jid)
    return now - _last_reconciled.get(key, _EPOCH) < RECONCILE_COOLDOWN


async def _reconcile_inbound(
    ch: Channel,
    canonical_jid: str,
    target_jid: str,
    group: WorkspaceProfile | None,
    inbound_cursor: str,
    deps: ReconcilerDeps,
) -> tuple[str, int] | None:
    """Fetch and ingest missed inbound messages.

    Returns (new_inbound_cursor, recovered_count), or None if the fetch itself
    failed (caller should skip outbound retry and the cooldown/cursor update
    for this pair, so the next cycle retries without waiting out the cooldown).
    """
    logger.debug(
        "reconciler_trace",
        step="fetch_inbound",
        channel=ch.name,
        jid=canonical_jid,
        cursor=inbound_cursor[:30] if inbound_cursor else "none",
    )
    result = await _fetch_inbound_result(ch, target_jid, canonical_jid, inbound_cursor)
    if result is None:
        return None

    remote_messages = result.messages
    logger.debug(
        "reconciler_trace",
        step="fetch_result",
        channel=ch.name,
        jid=canonical_jid,
        msg_count=len(remote_messages),
        high_water_mark=result.high_water_mark[:30] if result.high_water_mark else "none",
    )
    # Seed with high-water mark so the cursor advances past bot-only
    # pages even when no user messages are found.
    new_inbound_cursor = (
        result.high_water_mark if result.high_water_mark > inbound_cursor else inbound_cursor
    )
    new_inbound_cursor, recovered = await _ingest_remote_messages(
        ch,
        canonical_jid,
        group,
        remote_messages,
        new_inbound_cursor,
        deps,
    )

    logger.debug(
        "reconciler_trace",
        step="cursor_advance",
        jid=canonical_jid,
        old_cursor=inbound_cursor[:30] if inbound_cursor else "none",
        new_cursor=new_inbound_cursor[:30] if new_inbound_cursor else "none",
        will_advance=new_inbound_cursor != inbound_cursor,
    )
    return new_inbound_cursor, recovered


async def _fetch_inbound_result(
    ch: Channel,
    target_jid: str,
    canonical_jid: str,
    inbound_cursor: str,
) -> InboundFetchResult | None:
    try:
        return await ch.fetch_inbound_since(target_jid, inbound_cursor)
    except Exception as exc:  # noqa: BLE001, RUF100 - channel fetch is a remote boundary; treat failures as retryable skip.
        logger.warning(
            "fetch_inbound_since failed", channel=ch.name, jid=canonical_jid, error=str(exc)
        )
        return None


async def _ingest_remote_messages(
    ch: Channel,
    canonical_jid: str,
    group: WorkspaceProfile | None,
    remote_messages: list[NewMessage],
    new_inbound_cursor: str,
    deps: ReconcilerDeps,
) -> tuple[str, int]:
    recovered = 0
    for msg in remote_messages:
        new_inbound_cursor, did_recover = await _ingest_remote_message(
            ch,
            canonical_jid,
            group,
            msg,
            new_inbound_cursor,
            deps,
        )
        recovered += int(did_recover)
    return new_inbound_cursor, recovered


async def _ingest_remote_message(
    ch: Channel,
    canonical_jid: str,
    group: WorkspaceProfile | None,
    msg: NewMessage,
    new_inbound_cursor: str,
    deps: ReconcilerDeps,
) -> tuple[str, bool]:
    from pynchy.config.access import filter_allowed_messages

    # Remap chat_jid to canonical (the channel returned channel-native JIDs)
    msg.chat_jid = canonical_jid
    exists = await message_exists(msg.id, canonical_jid)
    logger.debug(
        "reconciler_trace",
        step="msg_check",
        jid=canonical_jid,
        msg_id=msg.id,
        msg_ts=msg.timestamp[:30] if msg.timestamp else "none",
        exists=exists,
        sender=msg.sender,
    )
    if exists:
        return _advance_inbound_cursor(new_inbound_cursor, msg.timestamp), False

    if not filter_allowed_messages([msg], group, ch.name):
        logger.debug(
            "reconciler_skip_sender", channel=ch.name, jid=canonical_jid, sender=msg.sender
        )
        return _advance_inbound_cursor(new_inbound_cursor, msg.timestamp), False

    logger.debug("reconciler_trace", step="ingesting", jid=canonical_jid, msg_id=msg.id)
    await deps._ingest_user_message(msg, source_channel=ch.name)
    await deps.start_interactive_turn(canonical_jid)
    return _advance_inbound_cursor(new_inbound_cursor, msg.timestamp), True


def _advance_inbound_cursor(cursor: str, timestamp: str) -> str:
    return timestamp if timestamp > cursor else cursor


async def _deliver_pending_outbound_row(
    ch: Channel, target_jid: str, row: Any, outbound_cursor: str
) -> str:
    event = OutboundEvent(type=OutboundEventType.TEXT, content=row.content)
    await ch.send_event(target_jid, event)
    await mark_delivered(row.ledger_id, ch.name)
    if row.timestamp > outbound_cursor:
        outbound_cursor = row.timestamp
    return outbound_cursor


async def _retry_outbound(
    ch: Channel, canonical_jid: str, target_jid: str, outbound_cursor: str
) -> tuple[str, int]:
    """Retry pending outbound deliveries. Returns (new_outbound_cursor, retried_count)."""
    pending = await get_pending_outbound(ChannelName(ch.name), ChatJid(canonical_jid))
    new_outbound_cursor = outbound_cursor
    retried = 0
    for row in pending:
        try:
            new_outbound_cursor = await _deliver_pending_outbound_row(
                ch,
                target_jid,
                row,
                new_outbound_cursor,
            )
        except Exception as exc:  # noqa: BLE001, RUF100 - outbound retry is best-effort and stops after the first delivery failure.
            logger.warning(
                "outbound retry failed", channel=ch.name, ledger_id=row.ledger_id, error=str(exc)
            )
            await mark_delivery_error(row.ledger_id, ch.name, str(exc))
            break  # preserve ordering — don't skip ahead
        else:
            retried += 1
    return new_outbound_cursor, retried


async def reconcile_all_channels(deps: ReconcilerDeps) -> None:
    """Reconcile inbound history and retry pending outbound for all channels.

    Runs at boot and periodically from the message polling loop.
    """
    now = datetime.now(UTC)
    recovered = 0
    retried = 0

    for ch in deps.channels:
        for canonical_jid in deps.workspaces:
            pair_result = await _reconcile_channel_pair(deps, ch, canonical_jid, now)
            if pair_result is None:
                continue
            pair_recovered, pair_retried = pair_result
            recovered += pair_recovered
            retried += pair_retried

    _log_reconciliation_summary(recovered, retried)

    # GC cursors for channels absent from the active set (e.g. after a rename)
    active_names = {ChannelName(ch.name) for ch in deps.channels}
    pruned = await prune_stale_cursors(active_names)
    if pruned:
        logger.info("Pruned stale cursors", count=pruned)


async def _reconcile_channel_pair(
    deps: ReconcilerDeps,
    ch: Channel,
    canonical_jid: str,
    now: datetime,
) -> tuple[int, int] | None:
    group = deps.workspaces.get(canonical_jid)
    if _should_skip_pair(ch, canonical_jid, group, now):
        return None

    target_jid = canonical_jid
    logger.debug(
        "reconciler_trace",
        step="past_cooldown",
        channel=ch.name,
        jid=canonical_jid,
        target_jid=target_jid,
    )

    inbound_cursor = await _inbound_cursor(ch.name, canonical_jid, now)
    inbound_result = await _reconcile_inbound(
        ch, canonical_jid, target_jid, group, inbound_cursor, deps
    )
    if inbound_result is None:
        return None
    new_inbound_cursor, pair_recovered = inbound_result

    outbound_cursor = await get_channel_cursor(
        ChannelName(ch.name), ChatJid(canonical_jid), "outbound"
    )
    new_outbound_cursor, pair_retried = await _retry_outbound(
        ch, canonical_jid, target_jid, outbound_cursor
    )
    await _advance_pair_cursors(
        ch.name,
        canonical_jid,
        inbound_cursor=inbound_cursor,
        new_inbound_cursor=new_inbound_cursor,
        outbound_cursor=outbound_cursor,
        new_outbound_cursor=new_outbound_cursor,
    )
    _last_reconciled[ch.name, canonical_jid] = now
    return pair_recovered, pair_retried


async def _inbound_cursor(channel_name: str, canonical_jid: str, now: datetime) -> str:
    inbound_cursor = await get_channel_cursor(
        ChannelName(channel_name), ChatJid(canonical_jid), "inbound"
    )
    if inbound_cursor:
        return inbound_cursor
    # No cursor yet — channel was never reconciled (e.g. a
    # Slack-native workspace that was never reconciled).
    # Seed with a lookback so Socket Mode drops are recoverable
    # from the first cycle onward.  The cursor advances naturally
    # as messages are walked.
    return (now - _INITIAL_LOOKBACK).isoformat()


async def _advance_pair_cursors(
    channel_name: str,
    canonical_jid: str,
    *,
    inbound_cursor: str,
    new_inbound_cursor: str,
    outbound_cursor: str,
    new_outbound_cursor: str,
) -> None:
    await advance_cursors_atomic(
        ChannelName(channel_name),
        ChatJid(canonical_jid),
        inbound=new_inbound_cursor if new_inbound_cursor != inbound_cursor else None,
        outbound=new_outbound_cursor if new_outbound_cursor != outbound_cursor else None,
    )


def _log_reconciliation_summary(recovered: int, retried: int) -> None:
    if recovered:
        logger.info("Recovered missed channel messages", count=recovered)
    if retried:
        logger.info("Retried pending outbound deliveries", count=retried)
    if not recovered and not retried:
        logger.debug("Reconciliation complete, nothing to recover")


def reset_cooldowns() -> None:
    """Clear all cooldown state (useful for tests)."""
    _last_reconciled.clear()
