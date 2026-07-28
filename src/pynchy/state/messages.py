"""Message storage and retrieval."""

from __future__ import annotations

import json
from collections.abc import (
    Sequence,  # noqa: TC003 - beartype resolves this runtime annotation.
)
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiosqlite import Row
else:
    Row = Any

from pynchy.plugins.api import NewMessage
from pynchy.state.chat_parents import ensure_chat_parent
from pynchy.state.connection import _get_db, atomic_write


def _row_to_message(row: Row) -> NewMessage:
    """Convert a database row to a NewMessage."""
    metadata_str = row["metadata"]

    try:
        is_from_me: bool | None = bool(row["is_from_me"])
    except (KeyError, IndexError):
        is_from_me = None

    return NewMessage(
        id=row["id"],
        chat_jid=row["chat_jid"],
        sender=row["sender"],
        sender_name=row["sender_name"],
        content=row["content"],
        timestamp=row["timestamp"],
        is_from_me=is_from_me,
        message_type=row["message_type"] or "user",
        metadata=json.loads(metadata_str) if metadata_str else None,
    )


async def store_message(msg: NewMessage, message_type: str = "user") -> None:
    """Store a message with full content in SQLite.

    Args:
        msg: The message to store
        message_type: One of 'user', 'assistant', 'system', 'host', 'tool_result'
    """
    await store_message_direct(
        message_id=msg.id,
        chat_jid=msg.chat_jid,
        sender=msg.sender,
        sender_name=msg.sender_name,
        content=msg.content,
        timestamp=msg.timestamp,
        is_from_me=msg.is_from_me or False,
        message_type=message_type,
        metadata=msg.metadata,
    )


async def store_message_direct(  # noqa: PLR0913 - DB row writer keeps the message columns explicit.
    *,
    message_id: str,
    chat_jid: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool,
    message_type: str = "user",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Store a message directly with explicit fields.

    Args:
        message_type: One of 'user', 'assistant', 'system', 'host', 'tool_result'
        metadata: Optional metadata dict (e.g., severity, tool_use_id, etc.)
    """
    metadata_json = json.dumps(metadata) if metadata else None
    async with atomic_write() as db:
        await ensure_chat_parent(db, chat_jid, timestamp)
        await db.execute(
            "INSERT OR REPLACE INTO messages "
            "(id, chat_jid, sender, sender_name, content, timestamp, is_from_me, "
            "message_type, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                chat_jid,
                sender,
                sender_name,
                content,
                timestamp,
                1 if is_from_me else 0,
                message_type,
                metadata_json,
            ),
        )


async def message_exists(msg_id: str, chat_jid: str) -> bool:
    """Check if a message with the given ID and chat JID already exists."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT 1 FROM messages WHERE id = ? AND chat_jid = ? LIMIT 1",
        (msg_id, chat_jid),
    )
    return await cursor.fetchone() is not None


async def mark_message_as_host(
    message_id: str,
    chat_jid: str,
    *,
    deferred_control: bool = False,
) -> None:
    """Persistently exclude one consumed human control message from agent context."""
    async with atomic_write() as db:
        metadata_cursor = await db.execute(
            "SELECT metadata FROM messages WHERE id = ? AND chat_jid = ?",
            (message_id, chat_jid),
        )
        row = await metadata_cursor.fetchone()
        if row is None:
            raise ValueError("Control message disappeared before it could be consumed")
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        if not isinstance(metadata, dict):
            raise TypeError("Control message metadata has an invalid persisted shape")
        if deferred_control:
            metadata["deferred_host_control"] = True
        cursor = await db.execute(
            "UPDATE messages SET message_type = 'host', metadata = ? WHERE id = ? AND chat_jid = ?",
            (json.dumps(metadata), message_id, chat_jid),
        )
        if cursor.rowcount != 1:
            raise ValueError("Control message disappeared before it could be consumed")


async def get_new_messages(jids: list[str], last_timestamp: str) -> tuple[list[NewMessage], str]:
    """Get inbound messages across multiple groups since a timestamp."""
    if not jids:
        return [], last_timestamp

    db = _get_db()
    placeholders = ",".join("?" for _ in jids)
    # S608 audit: only the number of SQLite value placeholders is dynamic.
    sql = f"""
        SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me,
               message_type, metadata
        FROM messages
        WHERE timestamp > ? AND chat_jid IN ({placeholders})
              AND is_from_me = 0
        ORDER BY timestamp
    """  # noqa: S608
    cursor = await db.execute(sql, [last_timestamp, *jids])
    rows = await cursor.fetchall()

    messages = [_row_to_message(row) for row in rows]
    new_timestamp = max((msg.timestamp for msg in messages), default=last_timestamp)
    return messages, new_timestamp


async def get_messages_since(
    chat_jid: str,
    since_timestamp: str | None,
    *,
    body_reader: object | None = None,
) -> list[NewMessage]:
    """Get inbound messages for a specific chat since a timestamp."""
    del body_reader

    db = _get_db()
    sql = """
        SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me,
               message_type, metadata
        FROM messages
        WHERE chat_jid = ?
              AND is_from_me = 0
    """
    params: list[object] = [chat_jid]
    if since_timestamp is not None:
        sql += " AND timestamp > ?"
        params.append(since_timestamp)
    sql += " ORDER BY timestamp"
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()

    return [_row_to_message(row) for row in rows]


async def get_messaging_stats() -> dict[str, int | str | None]:
    """Return aggregate messaging stats for the status endpoint.

    Combines inbound message counts, outbound ledger counts, and pending
    delivery counts into a single efficient query (scalar subqueries).

    Touches ``outbound_ledger`` and ``outbound_deliveries`` tables in
    addition to ``messages`` — kept here rather than split across modules
    because it's a single cross-cutting stats query.
    """
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM messages WHERE is_from_me = 0) AS total_inbound,
            (SELECT COUNT(*) FROM outbound_ledger) AS total_outbound,
            (SELECT MAX(timestamp) FROM messages WHERE is_from_me = 0) AS last_received_at,
            (SELECT MAX(timestamp) FROM outbound_ledger) AS last_sent_at,
            (SELECT COUNT(*) FROM outbound_deliveries WHERE delivered_at IS NULL)
                AS pending_deliveries
        """
    )
    row = await cursor.fetchone()
    return {
        "total_inbound": row["total_inbound"] if row else 0,
        "total_outbound": row["total_outbound"] if row else 0,
        "last_received_at": row["last_received_at"] if row else None,
        "last_sent_at": row["last_sent_at"] if row else None,
        "pending_deliveries": row["pending_deliveries"] if row else 0,
    }


async def get_latest_inbound_timestamp(chat_jids: Sequence[str]) -> str | None:
    """Return the newest persisted inbound timestamp for the selected chats.

    This aggregate intentionally avoids loading message bodies. Host health
    surfaces use it to report Pynchy ingestion freshness without exposing a
    conversation or changing any provider read state.
    """
    if not chat_jids:
        return None
    db = _get_db()
    placeholders = ",".join("?" for _ in chat_jids)
    # S608 audit: only the number of SQLite value placeholders is dynamic.
    cursor = await db.execute(
        f"SELECT MAX(timestamp) AS latest FROM messages"  # noqa: S608
        f" WHERE is_from_me = 0 AND chat_jid IN ({placeholders})",
        tuple(chat_jids),
    )
    row = await cursor.fetchone()
    return row["latest"] if row and row["latest"] else None


async def prune_messages_by_sender(sender: str, before_timestamp: str) -> int:
    """Delete messages by sender older than a timestamp.

    Only deletes rows matching the given sender — other messages are untouched.
    Returns the number of rows deleted.
    """
    db = _get_db()
    cursor = await db.execute(
        "DELETE FROM messages WHERE sender = ? AND timestamp < ?",
        (sender, before_timestamp),
    )
    await db.commit()
    return cursor.rowcount


async def get_chat_history(
    chat_jid: str,
    limit: int = 50,
    *,
    body_reader: object | None = None,
) -> list[NewMessage]:
    """Get recent messages for a chat, including bot responses. Newest last.

    Respects the cleared_at boundary — messages before it are hidden.
    """
    del body_reader

    db = _get_db()
    cleared_cursor = await db.execute("SELECT cleared_at FROM chats WHERE jid = ?", (chat_jid,))
    cleared_row = await cleared_cursor.fetchone()
    cleared_at = cleared_row["cleared_at"] if cleared_row and cleared_row["cleared_at"] else None

    if cleared_at:
        cursor = await db.execute(
            """
            SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me,
                   message_type, metadata
            FROM messages
            WHERE chat_jid = ? AND timestamp > ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (chat_jid, cleared_at, limit),
        )
    else:
        cursor = await db.execute(
            """
            SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me,
                   message_type, metadata
            FROM messages
            WHERE chat_jid = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (chat_jid, limit),
        )
    rows = await cursor.fetchall()

    return [_row_to_message(row) for row in reversed(list(rows))]
