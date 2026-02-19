"""Per-channel bidirectional cursor CRUD.

Each (channel_name, chat_jid, direction) triple tracks how far we've synced
inbound or outbound for that channel-group pair.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.db._connection import _get_db, get_write_lock


async def get_channel_cursor(
    channel_name: str, chat_jid: str, direction: str
) -> str:
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
    channel_name: str, chat_jid: str, direction: str, value: str
) -> None:
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

    Holds the write lock for the full duration so no concurrent coroutine can
    interleave DML and have it swept up in — or wiped out by — our rollback.
    """
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    async with get_write_lock():
        try:
            for direction, value in [("inbound", inbound), ("outbound", outbound)]:
                if value:
                    await db.execute(
                        "INSERT OR REPLACE INTO channel_cursors"
                        " (channel_name, chat_jid, direction, cursor_value, updated_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (channel_name, chat_jid, direction, value, now),
                    )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
