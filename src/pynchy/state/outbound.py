"""Outbound ledger — tracks messages that need delivery to channels.

Content is stored once in outbound_ledger; per-channel delivery status is
normalized into outbound_deliveries.  The reconciler retries rows where
delivered_at IS NULL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pynchy.identifiers import (
    ChannelName,
    ChatJid,
)
from pynchy.state.chat_parents import ensure_chat_parent
from pynchy.state.connection import _get_db, atomic_write

_OUTBOUND_LEDGER_ID_MISSING_ERROR = "INSERT INTO outbound_ledger did not return a row id"


class OutboundDeliveryOperation(StrEnum):
    """Remote mutation represented by one per-channel ledger delivery."""

    POST = "post"
    EDIT = "edit"
    FALLBACK_POST = "fallback_post"


@dataclass(frozen=True)
class OutboundDelivery:
    """How one channel should deliver an outbound ledger entry."""

    channel_name: ChannelName
    operation: OutboundDeliveryOperation = OutboundDeliveryOperation.POST
    remote_message_id: str | None = None


@dataclass
class PendingDelivery:
    """A row from the outbound ledger awaiting delivery to a specific channel."""

    ledger_id: int
    chat_jid: str
    content: str
    timestamp: str
    source: str
    operation: OutboundDeliveryOperation
    remote_message_id: str | None


async def record_outbound(
    chat_jid: ChatJid,
    content: str,
    source: str,
    channel_names: list[ChannelName],
) -> int:
    """Write an outbound message and create delivery rows for each channel.

    Returns the ledger row ID.
    """
    return await record_outbound_deliveries(
        chat_jid,
        content,
        source,
        [OutboundDelivery(channel_name=name) for name in channel_names],
    )


async def record_outbound_deliveries(
    chat_jid: ChatJid,
    content: str,
    source: str,
    deliveries: list[OutboundDelivery],
) -> int:
    """Write outbound content with explicit per-channel mutation semantics."""
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as db:
        await ensure_chat_parent(db, chat_jid, now)
        cursor = await db.execute(
            "INSERT INTO outbound_ledger (chat_jid, content, timestamp, source)"
            " VALUES (?, ?, ?, ?)",
            (chat_jid, content, now, source),
        )
        ledger_id = cursor.lastrowid
        if ledger_id is None:
            raise RuntimeError(_OUTBOUND_LEDGER_ID_MISSING_ERROR)
        for delivery in deliveries:
            await db.execute(
                "INSERT INTO outbound_deliveries"
                " (ledger_id, channel_name, operation, remote_message_id)"
                " VALUES (?, ?, ?, ?)",
                (
                    ledger_id,
                    delivery.channel_name,
                    delivery.operation,
                    delivery.remote_message_id,
                ),
            )
    return ledger_id


async def mark_delivered(ledger_id: int, channel_name: str) -> None:
    """Mark a delivery as successful."""
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as db:
        await db.execute(
            "UPDATE outbound_deliveries SET delivered_at = ?, error = NULL"
            " WHERE ledger_id = ? AND channel_name = ?",
            (now, ledger_id, channel_name),
        )


async def mark_delivery_succeeded(
    ledger_id: int,
    channel_name: str,
    operation: OutboundDeliveryOperation,
    remote_message_id: str | None,
) -> None:
    """Record the mutation that actually succeeded for one delivery attempt."""
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as db:
        await db.execute(
            "UPDATE outbound_deliveries"
            " SET delivered_at = ?, error = NULL, operation = ?, remote_message_id = ?"
            " WHERE ledger_id = ? AND channel_name = ?",
            (now, operation, remote_message_id, ledger_id, channel_name),
        )


async def mark_delivery_error(ledger_id: int, channel_name: str, error: str) -> None:
    """Record a delivery failure (leaves delivered_at NULL for retry)."""
    async with atomic_write() as db:
        await db.execute(
            "UPDATE outbound_deliveries SET error = ? WHERE ledger_id = ? AND channel_name = ?",
            (error, ledger_id, channel_name),
        )


async def get_pending_outbound(
    channel_name: ChannelName, chat_jid: ChatJid
) -> list[PendingDelivery]:
    """Get undelivered outbound messages for a (channel, group) pair.

    Ordered by ledger ID (creation order) to preserve message ordering.
    """
    db = _get_db()
    cursor = await db.execute(
        "SELECT ol.id, ol.chat_jid, ol.content, ol.timestamp, ol.source,"
        " od.operation, od.remote_message_id"
        " FROM outbound_deliveries od"
        " JOIN outbound_ledger ol ON od.ledger_id = ol.id"
        " WHERE od.channel_name = ? AND ol.chat_jid = ? AND od.delivered_at IS NULL"
        " ORDER BY ol.id",
        (channel_name, chat_jid),
    )
    rows = await cursor.fetchall()
    return [
        PendingDelivery(
            ledger_id=row["id"],
            chat_jid=row["chat_jid"],
            content=row["content"],
            timestamp=row["timestamp"],
            source=row["source"],
            operation=OutboundDeliveryOperation(row["operation"]),
            remote_message_id=row["remote_message_id"],
        )
        for row in rows
    ]


async def gc_delivered(max_age_hours: int = 24) -> int:
    """Delete ledger entries older than max_age where all channels delivered.

    Returns the number of ledger rows deleted.
    """
    db = _get_db()
    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
    # Find ledger IDs past the cutoff with no pending deliveries
    cursor = await db.execute(
        "SELECT ol.id FROM outbound_ledger ol"
        " WHERE ol.timestamp < ?"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM outbound_deliveries od"
        "   WHERE od.ledger_id = ol.id AND od.delivered_at IS NULL"
        " )",
        (cutoff,),
    )
    rows = await cursor.fetchall()
    ids = [row["id"] for row in rows]
    if not ids:
        return 0

    placeholders = ",".join("?" * len(ids))
    # S608 audit: only the number of SQLite value placeholders is dynamic.
    async with atomic_write() as wdb:
        await wdb.execute(
            f"DELETE FROM outbound_deliveries WHERE ledger_id IN ({placeholders})",  # noqa: S608
            ids,
        )
        await wdb.execute(
            f"DELETE FROM outbound_ledger WHERE id IN ({placeholders})",  # noqa: S608
            ids,
        )
    return len(ids)
