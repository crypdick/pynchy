"""Unified channel reconciliation — replaces _catch_up_channel_history().

Single code path for all channels.  Per-(channel, group) cooldown prevents
excessive API calls during rapid polling cycles.  Uses the alias table as
an implicit whitelist — no alias and no JID ownership = skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.db import (
    advance_cursors_atomic,
    get_channel_cursor,
    get_pending_outbound,
    mark_delivered,
    mark_delivery_error,
    message_exists,
    prune_stale_cursors,
)
from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.types import Channel, NewMessage, WorkspaceProfile

RECONCILE_COOLDOWN = timedelta(seconds=90)
_INITIAL_LOOKBACK = timedelta(hours=24)
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

# Module-level cooldown state (survives across calls within a process)
_last_reconciled: dict[tuple[str, str], datetime] = {}


class ReconcilerDeps(Protocol):
    """Minimal dependencies for the reconciler."""

    @property
    def channels(self) -> list[Channel]: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    def get_channel_jid(self, canonical_jid: str, channel_name: str) -> str | None: ...

    @property
    def queue(self) -> Any: ...

    async def _ingest_user_message(
        self, msg: NewMessage, *, source_channel: str | None = None
    ) -> None: ...


async def reconcile_all_channels(deps: ReconcilerDeps) -> None:
    """Reconcile inbound history and retry pending outbound for all channels.

    Replaces _catch_up_channel_history(). Runs at boot and periodically
    from the message polling loop.
    """
    now = datetime.now(UTC)
    recovered = 0
    retried = 0

    for ch in deps.channels:
        for canonical_jid in deps.workspaces:
            from pynchy.config_access import filter_allowed_messages, resolve_workspace_connection_name

            group = deps.workspaces.get(canonical_jid)
            if group is not None:
                expected = resolve_workspace_connection_name(group.folder)
                if expected and expected != ch.name:
                    logger.debug(
                        "connection_gate_skip",
                        channel=ch.name,
                        canonical_jid=canonical_jid,
                        expected=expected,
                    )
                    continue

            channel_jid = deps.get_channel_jid(canonical_jid, ch.name)
            if not channel_jid and not ch.owns_jid(canonical_jid):
                logger.debug(
                    "jid_ownership_skip",
                    channel=ch.name,
                    canonical_jid=canonical_jid,
                )
                continue

            target_jid = channel_jid or canonical_jid

            # --- Cooldown ---
            key = (ch.name, canonical_jid)
            if now - _last_reconciled.get(key, _EPOCH) < RECONCILE_COOLDOWN:
                continue

            # --- Inbound ---
            logger.info(
                "reconciler_trace",
                step="past_cooldown",
                channel=ch.name,
                jid=canonical_jid,
                target_jid=target_jid,
            )
            inbound_cursor = await get_channel_cursor(ch.name, canonical_jid, "inbound")
            if not inbound_cursor:
                # No cursor yet — channel was never reconciled (e.g. a
                # Slack-native workspace with no cross-channel aliases).
                # Seed with a lookback so Socket Mode drops are recoverable
                # from the first cycle onward.  The cursor advances
                # naturally as messages are walked.
                inbound_cursor = (now - _INITIAL_LOOKBACK).isoformat()

            logger.info(
                "reconciler_trace",
                step="fetch_inbound",
                channel=ch.name,
                jid=canonical_jid,
                cursor=inbound_cursor[:30] if inbound_cursor else "none",
            )
            try:
                remote_messages = await ch.fetch_inbound_since(target_jid, inbound_cursor)
            except Exception as exc:
                logger.warning(
                    "fetch_inbound_since failed",
                    channel=ch.name,
                    jid=canonical_jid,
                    error=str(exc),
                )
                continue

            logger.info(
                "reconciler_trace",
                step="fetch_result",
                channel=ch.name,
                jid=canonical_jid,
                msg_count=len(remote_messages),
            )
            new_inbound_cursor = inbound_cursor
            for msg in remote_messages:
                # Remap chat_jid to canonical (the channel returned channel-native JIDs)
                msg.chat_jid = canonical_jid
                if not await message_exists(msg.id, canonical_jid):
                    # Sender filter: match _route_incoming_group behavior.
                    # Admin groups bypass; non-admin groups check allowed_users.
                    if not filter_allowed_messages([msg], group, ch.name):
                        logger.info(
                            "reconciler_skip_sender",
                            channel=ch.name,
                            jid=canonical_jid,
                            sender=msg.sender,
                        )
                        if msg.timestamp > new_inbound_cursor:
                            new_inbound_cursor = msg.timestamp
                        continue
                    await deps._ingest_user_message(msg, source_channel=ch.name)
                    deps.queue.enqueue_message_check(canonical_jid)
                    recovered += 1
                if msg.timestamp > new_inbound_cursor:
                    new_inbound_cursor = msg.timestamp

            # --- Outbound retry ---
            pending = await get_pending_outbound(ch.name, canonical_jid)
            outbound_cursor = await get_channel_cursor(ch.name, canonical_jid, "outbound")
            new_outbound_cursor = outbound_cursor
            for row in pending:
                try:
                    await ch.send_message(target_jid, row.content)
                    await mark_delivered(row.ledger_id, ch.name)
                    if row.timestamp > new_outbound_cursor:
                        new_outbound_cursor = row.timestamp
                    retried += 1
                except Exception as exc:
                    await mark_delivery_error(row.ledger_id, ch.name, str(exc))
                    break  # preserve ordering — don't skip ahead

            # --- Atomic cursor update ---
            await advance_cursors_atomic(
                ch.name,
                canonical_jid,
                inbound=new_inbound_cursor if new_inbound_cursor != inbound_cursor else None,
                outbound=new_outbound_cursor if new_outbound_cursor != outbound_cursor else None,
            )
            _last_reconciled[key] = now

    if recovered:
        logger.info("Recovered missed channel messages", count=recovered)
    if retried:
        logger.info("Retried pending outbound deliveries", count=retried)
    if not recovered and not retried:
        logger.debug("Reconciliation complete, nothing to recover")

    # GC cursors for channels that no longer exist (e.g. after a rename)
    active_names = {ch.name for ch in deps.channels}
    pruned = await prune_stale_cursors(active_names)
    if pruned:
        logger.info("Pruned stale cursors", count=pruned)


def reset_cooldowns() -> None:
    """Clear all cooldown state (useful for tests)."""
    _last_reconciled.clear()
