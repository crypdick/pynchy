"""Unified channel reconciliation.

Single code path for all channels.  Per-(channel, group) cooldown prevents
excessive API calls during rapid polling cycles.  Channels that don't own
the canonical JID are skipped.
"""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves sender-policy annotations at runtime.
)
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from pynchy.host.orchestrator.messaging.sender import outbound_delivery_lock
from pynchy.identifiers import (
    ChannelName,
    ChatJid,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    Channel,
    NewMessage,
    OutboundEvent,
    OutboundEventType,
)
from pynchy.state.api import (  # beartype resolves this runtime annotation.
    OutboundDeliveryOperation,
    PendingDelivery,
    advance_cursors_atomic,
    get_channel_cursor,
    get_pending_outbound,
    mark_delivered,
    mark_delivery_error,
    mark_delivery_succeeded,
    message_exists,
    prune_stale_cursors,
)
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)

RECONCILE_COOLDOWN = timedelta(seconds=30)
_INITIAL_LOOKBACK = timedelta(hours=24)
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

# Module-level cooldown state (survives across calls within a process)
_last_reconciled: dict[tuple[str, str], datetime] = {}
_allowed_message_filter: (
    Callable[[list[NewMessage], WorkspaceProfile, str | None], list[NewMessage]] | None
) = None


def configure_allowed_message_filter(
    allowed_message_filter: Callable[
        [list[NewMessage], WorkspaceProfile, str | None], list[NewMessage]
    ],
) -> None:
    """Inject routed sender policy from application composition."""
    global _allowed_message_filter  # noqa: PLW0603 - one host process owns one sender policy.
    _allowed_message_filter = allowed_message_filter


def _messages_are_allowed(
    messages: list[NewMessage], group: WorkspaceProfile, channel_name: str
) -> bool:
    if _allowed_message_filter is None:
        raise RuntimeError("reconciler sender policy has not been configured")
    return bool(_allowed_message_filter(messages, group, channel_name))


@dataclass(frozen=True)
class _ReconcileInboundRequest:
    ch: Channel
    canonical_jid: str
    target_jid: str
    group: WorkspaceProfile
    inbound: _InboundCursor
    deps: ReconcilerDeps


@dataclass(frozen=True)
class _InboundCursor:
    fetch_since: str
    empty_fetch_cursor: str | None


@dataclass(frozen=True)
class _AdvancePairCursorsRequest:
    channel_name: str
    canonical_jid: str
    inbound_cursor: str
    new_inbound_cursor: str
    outbound_cursor: str
    new_outbound_cursor: str


@runtime_checkable
class ReconcilerDeps(Protocol):
    """Minimal dependencies for the reconciler."""

    @property
    def channels(self) -> list[Channel]: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def ingest_user_message(
        self, msg: NewMessage, *, source_channel: str | None = None
    ) -> None: ...

    async def start_interactive_turn(self, chat_jid: str) -> None: ...


def _owns_pair(ch: Channel, canonical_jid: str) -> bool:
    if not ch.owns_jid(canonical_jid):
        logger.debug("jid_ownership_skip", channel=ch.name, canonical_jid=canonical_jid)
        return False
    return True


def _is_on_cooldown(ch: Channel, canonical_jid: str, now: datetime) -> bool:
    key = (ch.name, canonical_jid)
    return now - _last_reconciled.get(key, _EPOCH) < RECONCILE_COOLDOWN


async def _reconcile_inbound(request: _ReconcileInboundRequest) -> tuple[str, int]:
    """Fetch and ingest missed inbound messages.

    Returns (new_inbound_cursor, recovered_count).
    """
    logger.debug(
        "reconciler_trace",
        step="fetch_inbound",
        channel=request.ch.name,
        jid=request.canonical_jid,
        cursor=request.inbound.fetch_since[:30],
    )
    result = await request.ch.fetch_inbound_since(
        request.target_jid,
        request.inbound.fetch_since,
    )

    remote_messages = result.messages
    logger.debug(
        "reconciler_trace",
        step="fetch_result",
        channel=request.ch.name,
        jid=request.canonical_jid,
        msg_count=len(remote_messages),
        high_water_mark=result.high_water_mark[:30] if result.high_water_mark else "none",
    )
    # Seed with high-water mark so the cursor advances past bot-only
    # pages even when no user messages are found.
    new_inbound_cursor = (
        result.high_water_mark
        if result.high_water_mark > request.inbound.fetch_since
        else request.inbound.fetch_since
    )
    if (
        not remote_messages
        and not result.high_water_mark
        and request.inbound.empty_fetch_cursor is not None
    ):
        new_inbound_cursor = request.inbound.empty_fetch_cursor
    new_inbound_cursor, recovered = await _ingest_remote_messages(
        request,
        remote_messages,
        new_inbound_cursor,
    )

    logger.debug(
        "reconciler_trace",
        step="cursor_advance",
        jid=request.canonical_jid,
        old_cursor=request.inbound.fetch_since[:30],
        new_cursor=new_inbound_cursor[:30] if new_inbound_cursor else "none",
        will_advance=new_inbound_cursor != request.inbound.fetch_since,
    )
    return new_inbound_cursor, recovered


async def _ingest_remote_messages(
    request: _ReconcileInboundRequest,
    remote_messages: list[NewMessage],
    new_inbound_cursor: str,
) -> tuple[str, int]:
    recovered = 0
    for msg in remote_messages:
        new_inbound_cursor, did_recover = await _ingest_remote_message(
            request,
            msg,
            new_inbound_cursor,
        )
        recovered += int(did_recover)
    return new_inbound_cursor, recovered


async def _ingest_remote_message(
    request: _ReconcileInboundRequest,
    msg: NewMessage,
    new_inbound_cursor: str,
) -> tuple[str, bool]:
    # Remap chat_jid to canonical (the channel returned channel-native JIDs)
    msg.chat_jid = request.canonical_jid
    exists = await message_exists(msg.id, request.canonical_jid)
    logger.debug(
        "reconciler_trace",
        step="msg_check",
        jid=request.canonical_jid,
        msg_id=msg.id,
        msg_ts=msg.timestamp[:30] if msg.timestamp else "none",
        exists=exists,
        sender=msg.sender,
    )
    if exists:
        return _advance_inbound_cursor(new_inbound_cursor, msg.timestamp), False

    if not _messages_are_allowed([msg], request.group, request.ch.name):
        logger.debug(
            "reconciler_skip_sender",
            channel=request.ch.name,
            jid=request.canonical_jid,
            sender=msg.sender,
        )
        return _advance_inbound_cursor(new_inbound_cursor, msg.timestamp), False

    logger.debug("reconciler_trace", step="ingesting", jid=request.canonical_jid, msg_id=msg.id)
    await request.deps.ingest_user_message(msg, source_channel=request.ch.name)
    await request.deps.start_interactive_turn(request.canonical_jid)
    return _advance_inbound_cursor(new_inbound_cursor, msg.timestamp), True


def _advance_inbound_cursor(cursor: str, timestamp: str) -> str:
    return timestamp if timestamp > cursor else cursor


async def _deliver_pending_outbound_row(
    ch: Channel, target_jid: str, row: PendingDelivery, outbound_cursor: str
) -> str:
    event = OutboundEvent(type=OutboundEventType.TEXT, content=row.content)
    update_event = getattr(ch, "update_event", None)
    if (
        row.operation is OutboundDeliveryOperation.EDIT
        and row.remote_message_id is not None
        and callable(update_event)
    ):
        try:
            await update_event(target_jid, row.remote_message_id, event)
        except Exception as exc:  # noqa: BLE001 - a stale or unavailable edit target falls back to a visible post.
            logger.warning(
                "Outbound edit retry failed, falling back to a post",
                channel=ch.name,
                ledger_id=row.ledger_id,
                error=str(exc),
            )
            await ch.send_event(target_jid, event)
            await mark_delivery_succeeded(
                row.ledger_id,
                ch.name,
                OutboundDeliveryOperation.FALLBACK_POST,
                None,
            )
        else:
            await mark_delivery_succeeded(
                row.ledger_id,
                ch.name,
                OutboundDeliveryOperation.EDIT,
                row.remote_message_id,
            )
    else:
        await ch.send_event(target_jid, event)
        if row.operation is OutboundDeliveryOperation.POST:
            await mark_delivered(row.ledger_id, ch.name)
        else:
            await mark_delivery_succeeded(
                row.ledger_id,
                ch.name,
                OutboundDeliveryOperation.FALLBACK_POST,
                None,
            )
    if row.timestamp > outbound_cursor:
        outbound_cursor = row.timestamp
    return outbound_cursor


async def _retry_outbound(
    ch: Channel, canonical_jid: str, target_jid: str, outbound_cursor: str
) -> tuple[str, int, str | None]:
    """Retry pending outbound deliveries and report an exhausted failure."""
    async with outbound_delivery_lock(canonical_jid):
        pending = await get_pending_outbound(ChannelName(ch.name), ChatJid(canonical_jid))
        new_outbound_cursor = outbound_cursor
        retried = 0
        failure = None
        for row in pending:
            try:
                new_outbound_cursor = await _deliver_pending_outbound_row(
                    ch,
                    target_jid,
                    row,
                    new_outbound_cursor,
                )
            except Exception as exc:  # noqa: BLE001 - outbound retry is best-effort and stops after the first delivery failure.
                logger.warning(
                    "outbound retry failed",
                    channel=ch.name,
                    ledger_id=row.ledger_id,
                    error=str(exc),
                )
                await mark_delivery_error(row.ledger_id, ch.name, str(exc))
                failure = f"outbound retry: {type(exc).__name__}: {exc}"
                break  # preserve ordering — don't skip ahead
            else:
                retried += 1
    return new_outbound_cursor, retried, failure


async def reconcile_all_channels(deps: ReconcilerDeps) -> None:
    """Reconcile inbound history and retry pending outbound for all channels.

    Runs at boot and periodically from the message polling loop.
    """
    now = datetime.now(UTC)
    recovered = 0
    retried = 0
    failures: list[str] = []
    # A cycle's candidate set is immutable because ingress may register
    # dynamic thread workspaces during pair reconciliation.
    canonical_jids = tuple(deps.workspaces)
    active_pairs: set[tuple[ChannelName, ChatJid]] = set()

    for ch in deps.channels:
        for canonical_jid in canonical_jids:
            try:
                if not _owns_pair(ch, canonical_jid):
                    continue
                active_pairs.add((ChannelName(ch.name), ChatJid(canonical_jid)))
                pair_result = await _reconcile_channel_pair(deps, ch, canonical_jid, now)
            # allow: exception-handling - isolate remote pair failures until the pass is complete.
            except Exception as exc:  # noqa: BLE001
                failure = f"{ch.name}/{canonical_jid}: {type(exc).__name__}: {exc}"
                failures.append(failure)
                logger.warning(
                    "Channel reconciliation pair failed",
                    channel=ch.name,
                    jid=canonical_jid,
                    error=str(exc),
                )
                continue
            if pair_result is None:
                continue
            pair_recovered, pair_retried, pair_failure = pair_result
            recovered += pair_recovered
            retried += pair_retried
            if pair_failure is not None:
                failures.append(f"{ch.name}/{canonical_jid}: {pair_failure}")

    _log_reconciliation_summary(recovered, retried)

    pruned = await prune_stale_cursors(active_pairs)
    if pruned:
        logger.info("Pruned stale cursors", count=pruned)
    if failures:
        raise RuntimeError(
            f"Channel reconciliation failed for {len(failures)} pair(s): {'; '.join(failures)}"
        )


async def _reconcile_channel_pair(
    deps: ReconcilerDeps,
    ch: Channel,
    canonical_jid: str,
    now: datetime,
) -> tuple[int, int, str | None] | None:
    group = deps.workspaces[canonical_jid]
    if _is_on_cooldown(ch, canonical_jid, now):
        return None

    target_jid = canonical_jid
    logger.debug(
        "reconciler_trace",
        step="past_cooldown",
        channel=ch.name,
        jid=canonical_jid,
        target_jid=target_jid,
    )

    inbound = await _inbound_cursor(ch.name, canonical_jid, now)
    inbound_result = await _reconcile_inbound(
        _ReconcileInboundRequest(
            ch=ch,
            canonical_jid=canonical_jid,
            target_jid=target_jid,
            group=group,
            inbound=inbound,
            deps=deps,
        )
    )
    new_inbound_cursor, pair_recovered = inbound_result

    outbound_cursor = await get_channel_cursor(
        ChannelName(ch.name), ChatJid(canonical_jid), "outbound"
    )
    new_outbound_cursor, pair_retried, pair_failure = await _retry_outbound(
        ch, canonical_jid, target_jid, outbound_cursor
    )
    await _advance_pair_cursors(
        _AdvancePairCursorsRequest(
            channel_name=ch.name,
            canonical_jid=canonical_jid,
            inbound_cursor=inbound.fetch_since,
            new_inbound_cursor=new_inbound_cursor,
            outbound_cursor=outbound_cursor,
            new_outbound_cursor=new_outbound_cursor,
        )
    )
    if pair_failure is None:
        _last_reconciled[ch.name, canonical_jid] = now
    return pair_recovered, pair_retried, pair_failure


async def _inbound_cursor(
    channel_name: str, canonical_jid: str, poll_started_at: datetime
) -> _InboundCursor:
    inbound_cursor = await get_channel_cursor(
        ChannelName(channel_name), ChatJid(canonical_jid), "inbound"
    )
    if inbound_cursor:
        return _InboundCursor(fetch_since=inbound_cursor, empty_fetch_cursor=None)
    # No cursor yet — channel was never reconciled (e.g. a
    # Slack-native workspace that was never reconciled).
    # Seed with a lookback so Socket Mode drops are recoverable
    # from the first cycle onward. A successful empty result records
    # poll start so messages arriving during the fetch remain eligible.
    return _InboundCursor(
        fetch_since=(poll_started_at - _INITIAL_LOOKBACK).isoformat(),
        empty_fetch_cursor=poll_started_at.isoformat(),
    )


async def _advance_pair_cursors(request: _AdvancePairCursorsRequest) -> None:
    await advance_cursors_atomic(
        ChannelName(request.channel_name),
        ChatJid(request.canonical_jid),
        inbound=(
            request.new_inbound_cursor
            if request.new_inbound_cursor != request.inbound_cursor
            else None
        ),
        outbound=(
            request.new_outbound_cursor
            if request.new_outbound_cursor != request.outbound_cursor
            else None
        ),
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
