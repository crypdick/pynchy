"""Chat metadata operations."""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.db._connection import _get_db


async def set_chat_cleared_at(chat_jid: str, timestamp: str) -> None:
    """Mark a chat as cleared at the given timestamp. Messages before this are hidden."""
    db = _get_db()
    await db.execute("UPDATE chats SET cleared_at = ? WHERE jid = ?", (timestamp, chat_jid))
    await db.commit()


async def store_chat_metadata(chat_jid: str, timestamp: str, name: str | None = None) -> None:
    """Store chat metadata only (no message content)."""
    db = _get_db()
    if name:
        await db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                name = excluded.name,
                last_message_time = MAX(last_message_time, excluded.last_message_time)
            """,
            (chat_jid, name, timestamp),
        )
    else:
        await db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                last_message_time = MAX(last_message_time, excluded.last_message_time)
            """,
            (chat_jid, chat_jid, timestamp),
        )
    await db.commit()


async def update_chat_name(chat_jid: str, name: str) -> None:
    """Update chat name without changing timestamp for existing chats."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """
        INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
        ON CONFLICT(jid) DO UPDATE SET name = excluded.name
        """,
        (chat_jid, name, now),
    )
    await db.commit()


async def get_all_chats() -> list[dict[str, str]]:
    """Get all known chats, ordered by most recent activity."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT jid, name, last_message_time FROM chats ORDER BY last_message_time DESC"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_last_group_sync() -> str | None:
    """Get timestamp of last group metadata sync."""
    db = _get_db()
    cursor = await db.execute("SELECT last_message_time FROM chats WHERE jid = '__group_sync__'")
    row = await cursor.fetchone()
    return row["last_message_time"] if row else None


async def set_last_group_sync() -> None:
    """Record that group metadata was synced."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO chats (jid, name, last_message_time) "
        "VALUES ('__group_sync__', '__group_sync__', ?)",
        (now,),
    )
    await db.commit()
