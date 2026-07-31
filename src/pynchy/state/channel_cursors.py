"""Per-channel bidirectional cursor CRUD.

Each (channel_name, chat_jid, direction) triple tracks how far we've synced
inbound or outbound for that channel-group pair.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.identifiers import (  # beartype resolves these runtime annotations.
    ChannelName,
    ChatJid,
)
from pynchy.state.connection import _get_db, atomic_write


async def get_channel_cursor(channel_name: ChannelName, chat_jid: ChatJid, direction: str) -> str:
    """Return the cursor value, or empty string if not yet tracked."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT cursor_value FROM channel_cursors"
        " WHERE channel_name = ? AND chat_jid = ? AND direction = ?",
        (channel_name, chat_jid, direction),
    )
    row = await cursor.fetchone()
    return row["cursor_value"] if row else ""


async def set_channel_cursor(
    channel_name: ChannelName, chat_jid: ChatJid, direction: str, value: str
) -> None:
    """Upsert a single cursor value."""
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as db:
        await db.execute(
            "INSERT OR REPLACE INTO channel_cursors"
            " (channel_name, chat_jid, direction, cursor_value, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (channel_name, chat_jid, direction, value, now),
        )


async def advance_cursors_atomic(
    channel_name: ChannelName,
    chat_jid: ChatJid,
    *,
    inbound: str | None = None,
    outbound: str | None = None,
) -> None:
    """Atomically advance inbound and/or outbound cursors in one transaction.

    Forward-only: if the stored cursor is already ahead of *value*, the
    stored value is kept.  ISO-8601 timestamp strings compare correctly
    with SQLite's ``MAX()`` because they sort lexicographically.
    """
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as db:
        for direction, value in [("inbound", inbound), ("outbound", outbound)]:
            if value:
                await db.execute(
                    "INSERT INTO channel_cursors"
                    " (channel_name, chat_jid, direction, cursor_value, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(channel_name, chat_jid, direction)"
                    " DO UPDATE SET"
                    "   cursor_value = MAX(excluded.cursor_value, channel_cursors.cursor_value),"
                    "   updated_at = excluded.updated_at",
                    (channel_name, chat_jid, direction, value, now),
                )


async def prune_stale_cursors(
    active_pairs: set[tuple[ChannelName, ChatJid]],
) -> int:
    """Delete cursors outside the eligible channel-workspace pairs.

    An empty runtime-derived set is not authoritative evidence that no
    configured pair exists, so it is a fail-safe no-op.

    Returns the number of rows deleted.
    """
    if not active_pairs:
        return 0
    async with atomic_write() as db:
        cursor = await db.execute("SELECT DISTINCT channel_name, chat_jid FROM channel_cursors")
        stored_pairs = {
            (
                ChannelName(row["channel_name"]),
                ChatJid(row["chat_jid"]),
            )
            for row in await cursor.fetchall()
        }
        stale_pairs = stored_pairs - active_pairs
        if not stale_pairs:
            return 0
        deleted = await db.executemany(
            "DELETE FROM channel_cursors WHERE channel_name = ? AND chat_jid = ?",
            stale_pairs,
        )
        return deleted.rowcount
