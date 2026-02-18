"""Message storage and retrieval."""

from __future__ import annotations

import json
from typing import Any

from pynchy.db._connection import _get_db
from pynchy.types import NewMessage


def _row_to_message(row) -> NewMessage:
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
    """Store a message with full content.

    Args:
        msg: The message to store
        message_type: One of 'user', 'assistant', 'system', 'host', 'tool_result'
    """
    await store_message_direct(
        id=msg.id,
        chat_jid=msg.chat_jid,
        sender=msg.sender,
        sender_name=msg.sender_name,
        content=msg.content,
        timestamp=msg.timestamp,
        is_from_me=msg.is_from_me or False,
        message_type=message_type,
    )


async def store_message_direct(
    *,
    id: str,
    chat_jid: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool,
    message_type: str = "user",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Store a message directly (for non-WhatsApp channels).

    Args:
        message_type: One of 'user', 'assistant', 'system', 'host', 'tool_result'
        metadata: Optional metadata dict (e.g., severity, tool_use_id, etc.)
    """
    db = _get_db()
    metadata_json = json.dumps(metadata) if metadata else None
    await db.execute(
        "INSERT OR REPLACE INTO messages "
        "(id, chat_jid, sender, sender_name, content, timestamp, is_from_me, "
        "message_type, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id,
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
    await db.commit()


async def message_exists(msg_id: str, chat_jid: str) -> bool:
    """Check if a message with the given ID and chat JID already exists."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT 1 FROM messages WHERE id = ? AND chat_jid = ? LIMIT 1",
        (msg_id, chat_jid),
    )
    return await cursor.fetchone() is not None


async def get_new_messages(jids: list[str], last_timestamp: str) -> tuple[list[NewMessage], str]:
    """Get new messages across multiple groups since a timestamp."""
    if not jids:
        return [], last_timestamp

    db = _get_db()
    placeholders = ",".join("?" for _ in jids)
    sql = f"""
        SELECT id, chat_jid, sender, sender_name, content, timestamp, message_type, metadata
        FROM messages
        WHERE timestamp > ? AND chat_jid IN ({placeholders})
              AND is_from_me = 0
        ORDER BY timestamp
    """
    cursor = await db.execute(sql, [last_timestamp, *jids])
    rows = await cursor.fetchall()

    messages = [_row_to_message(row) for row in rows]

    new_timestamp = last_timestamp
    for msg in messages:
        if msg.timestamp > new_timestamp:
            new_timestamp = msg.timestamp

    return messages, new_timestamp


async def get_messages_since(chat_jid: str, since_timestamp: str) -> list[NewMessage]:
    """Get messages for a specific chat since a timestamp, excluding bot and host messages."""
    db = _get_db()
    sql = """
        SELECT id, chat_jid, sender, sender_name, content, timestamp, message_type, metadata
        FROM messages
        WHERE chat_jid = ? AND timestamp > ?
              AND is_from_me = 0
        ORDER BY timestamp
    """
    cursor = await db.execute(sql, (chat_jid, since_timestamp))
    rows = await cursor.fetchall()

    return [_row_to_message(row) for row in rows]


async def get_chat_history(chat_jid: str, limit: int = 50) -> list[NewMessage]:
    """Get recent messages for a chat, including bot responses. Newest last.

    Respects the cleared_at boundary — messages before it are hidden.
    """
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

    return [_row_to_message(row) for row in reversed(rows)]
