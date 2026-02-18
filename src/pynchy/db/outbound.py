"""Outbound ledger — tracks messages that need delivery to channels.

Content is stored once in outbound_ledger; per-channel delivery status is
normalized into outbound_deliveries.  The reconciler retries rows where
delivered_at IS NULL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pynchy.db._connection import _get_db


@dataclass
class PendingDelivery:
    """A row from the outbound ledger awaiting delivery to a specific channel."""

    ledger_id: int
    chat_jid: str
    content: str
    timestamp: str
    source: str


async def record_outbound(
    chat_jid: str,
    content: str,
    source: str,
    channel_names: list[str],
) -> int:
    """Write a new outbound message and create delivery rows for each channel.

    Returns the ledger row ID.
    """
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "INSERT INTO outbound_ledger (chat_jid, content, timestamp, source)"
        " VALUES (?, ?, ?, ?)",
        (chat_jid, content, now, source),
    )
    ledger_id = cursor.lastrowid
    assert ledger_id is not None
    for ch_name in channel_names:
        await db.execute(
            "INSERT INTO outbound_deliveries (ledger_id, channel_name)"
            " VALUES (?, ?)",
            (ledger_id, ch_name),
        )
    await db.commit()
    return ledger_id


async def mark_delivered(ledger_id: int, channel_name: str) -> None:
    """Mark a delivery as successful."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE outbound_deliveries SET delivered_at = ?, error = NULL"
        " WHERE ledger_id = ? AND channel_name = ?",
        (now, ledger_id, channel_name),
    )
    await db.commit()


async def mark_delivery_error(ledger_id: int, channel_name: str, error: str) -> None:
    """Record a delivery failure (leaves delivered_at NULL for retry)."""
    db = _get_db()
    await db.execute(
        "UPDATE outbound_deliveries SET error = ?"
        " WHERE ledger_id = ? AND channel_name = ?",
        (error, ledger_id, channel_name),
    )
    await db.commit()


async def get_pending_outbound(channel_name: str, chat_jid: str) -> list[PendingDelivery]:
    """Get undelivered outbound messages for a (channel, group) pair.

    Ordered by ledger ID (creation order) to preserve message ordering.
    """
    db = _get_db()
    cursor = await db.execute(
        "SELECT ol.id, ol.chat_jid, ol.content, ol.timestamp, ol.source"
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
        )
        for row in rows
    ]


async def gc_delivered(max_age_hours: int = 24) -> int:
    """Delete ledger entries older than max_age where all channels delivered.

    Returns the number of ledger rows deleted.
    """
    db = _get_db()
    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
    # Find ledger IDs that are old enough AND have no pending deliveries
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
    await db.execute(f"DELETE FROM outbound_deliveries WHERE ledger_id IN ({placeholders})", ids)
    await db.execute(f"DELETE FROM outbound_ledger WHERE id IN ({placeholders})", ids)
    await db.commit()
    return len(ids)
