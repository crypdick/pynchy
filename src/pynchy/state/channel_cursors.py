"""Per-channel bidirectional cursor CRUD.

Each (channel_name, chat_jid, direction) triple tracks how far we've synced
inbound or outbound for that channel-group pair.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.state.connection import _get_db, atomic_write


async def get_channel_cursor(channel_name: str, chat_jid: str, direction: str) -> str:
    """Return the cursor value, or empty string if not yet tracked."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT cursor_value FROM channel_cursors"
        " WHERE channel_name = ? AND chat_jid = ? AND direction = ?",
        (channel_name, chat_jid, direction),
    )
    row = await cursor.fetchone()
    return row["cursor_value"] if row else ""


async def set_channel_cursor(channel_name: str, chat_jid: str, direction: str, value: str) -> None:
    """Upsert a single cursor value."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO channel_cursors"
        " (channel_name, chat_jid, direction, cursor_value, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (channel_name, chat_jid, direction, value, now),
    )
    await db.commit()


async def advance_cursors_atomic(
    channel_name: str,
    chat_jid: str,
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


async def prune_stale_cursors(active_channel_names: set[str]) -> int:
    """Delete cursors for channels no longer in the active set.

    Returns the number of rows deleted.
    """
    if not active_channel_names:
        return 0
    db = _get_db()
    placeholders = ",".join("?" for _ in active_channel_names)
    cursor = await db.execute(
        f"DELETE FROM channel_cursors WHERE channel_name NOT IN ({placeholders})",
        tuple(active_channel_names),
    )
    await db.commit()
    return cursor.rowcount
