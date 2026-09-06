"""Message storage and retrieval."""

from __future__ import annotations

import json
from collections.abc import (
    Sequence,
)
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from aiosqlite import Row
else:
    Row = Any

from pynchy.plugins.api import NewMessage
from pynchy.state.chat_parents import ensure_chat_parent
from pynchy.state.connection import _get_db, atomic_write

_SEQUENCE_CURSOR_PREFIX = "sequence:"


def _row_to_message(row: Row) -> NewMessage:
    """Convert a database row to a NewMessage."""
    metadata_str = row["metadata"]
    try:
        local_sequence = row["local_sequence"]
    except (IndexError, KeyError):
        local_sequence = None

    return NewMessage(
        id=row["id"],
        chat_jid=row["chat_jid"],
        sender=row["sender"],
        sender_name=row["sender_name"],
        content=row["content"],
        timestamp=row["timestamp"],
        is_from_me=bool(row["is_from_me"]),
        message_type=row["message_type"] or "user",
        metadata=json.loads(metadata_str) if metadata_str else None,
        local_sequence=local_sequence,
    )


def _cursor_sequence(cursor: str | None) -> int | None:
    if cursor is None or not cursor.startswith(_SEQUENCE_CURSOR_PREFIX):
        return None
    value = cursor.removeprefix(_SEQUENCE_CURSOR_PREFIX)
    return int(value) if value.isdecimal() else None


def message_cursor(message: NewMessage) -> str:
    """Return the durable local cursor for a stored message."""
    if message.local_sequence is None:
        return message.timestamp
    return f"{_SEQUENCE_CURSOR_PREFIX}{message.local_sequence}"


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
    msg.local_sequence = await _message_local_sequence(msg.id, msg.chat_jid)


async def _message_local_sequence(message_id: str, chat_jid: str) -> int:
    cursor = await _get_db().execute(
        "SELECT sequence FROM message_ingestion_order WHERE message_id = ? AND chat_jid = ?",
        (message_id, chat_jid),
    )
    row = cast("Row", await cursor.fetchone())
    return int(row["sequence"])


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
            "INSERT INTO messages "
            "(id, chat_jid, sender, sender_name, content, timestamp, is_from_me, "
            "message_type, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id, chat_jid) DO UPDATE SET "
            "sender = excluded.sender, sender_name = excluded.sender_name, "
            "content = excluded.content, timestamp = excluded.timestamp, "
            "is_from_me = excluded.is_from_me, message_type = excluded.message_type, "
            "metadata = excluded.metadata",
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
        await db.execute(
            "INSERT OR IGNORE INTO message_ingestion_order (message_id, chat_jid) VALUES (?, ?)",
            (message_id, chat_jid),
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
    """Get inbound messages across multiple groups since a durable cursor."""
    if not jids:
        return [], last_timestamp

    db = _get_db()
    placeholders = ",".join("?" for _ in jids)
    # S608 audit: only the number of SQLite value placeholders is dynamic.
    sequence = _cursor_sequence(last_timestamp)
    boundary = "ingestion.sequence > ?" if sequence is not None else "messages.timestamp > ?"
    sql = f"""
        SELECT messages.id, messages.chat_jid, messages.sender, messages.sender_name,
               messages.content, messages.timestamp, messages.is_from_me,
               messages.message_type, messages.metadata, ingestion.sequence AS local_sequence
        FROM messages
        JOIN message_ingestion_order AS ingestion
          ON ingestion.message_id = messages.id AND ingestion.chat_jid = messages.chat_jid
        WHERE {boundary} AND messages.chat_jid IN ({placeholders})
              AND messages.is_from_me = 0
        ORDER BY ingestion.sequence
    """  # noqa: S608
    cursor = await db.execute(sql, [sequence if sequence is not None else last_timestamp, *jids])
    rows = await cursor.fetchall()

    messages = [_row_to_message(row) for row in rows]
    new_cursor = message_cursor(messages[-1]) if messages else last_timestamp
    return messages, new_cursor


async def get_messages_since(
    chat_jid: str,
    since_timestamp: str | None,
    *,
    body_reader: object | None = None,
) -> list[NewMessage]:
    """Get inbound messages for a specific chat since a durable cursor."""
    del body_reader

    db = _get_db()
    # Routed projections stay in chat history after their durable claim completes or
    # is reclaimed, but only the projection for the active claim is pending input.
    sql = """
        SELECT messages.id, messages.chat_jid, messages.sender, messages.sender_name,
               messages.content, messages.timestamp, messages.is_from_me,
               messages.message_type, messages.metadata, ingestion.sequence AS local_sequence
        FROM messages
        JOIN message_ingestion_order AS ingestion
          ON ingestion.message_id = messages.id AND ingestion.chat_jid = messages.chat_jid
        WHERE messages.chat_jid = ?
              AND messages.is_from_me = 0
              AND (
                  json_extract(metadata, '$.conversation_claim_id') IS NULL
                  OR EXISTS (
                      SELECT 1
                      FROM conversation_deliveries AS delivery
                      WHERE delivery.claim_id =
                            json_extract(messages.metadata, '$.conversation_claim_id')
                            AND delivery.delivery_id = messages.id
                            AND delivery.conversation_id =
                                json_extract(messages.metadata, '$.conversation_id')
                            AND delivery.status = 'claimed'
                  )
              )
    """
    params: list[object] = [chat_jid]
    if since_timestamp is not None:
        sequence = _cursor_sequence(since_timestamp)
        if sequence is None:
            sql += " AND messages.timestamp > ?"
            params.append(since_timestamp)
        else:
            sql += " AND ingestion.sequence > ?"
            params.append(sequence)
    sql += " ORDER BY ingestion.sequence"
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()

    return [_row_to_message(row) for row in rows]


async def upgrade_message_cursor(jids: Sequence[str], cursor: str) -> str:
    """Convert one provider-time cursor to local ingestion order."""
    if not cursor or _cursor_sequence(cursor) is not None or not jids:
        return cursor
    db = _get_db()
    placeholders = ",".join("?" for _ in jids)
    # Anchor conservatively at the first message that produced the provider-time cursor.
    # Later-ingested older or equal-timestamp messages must remain pending.
    result = await db.execute(
        f"SELECT MIN(ingestion.sequence) AS sequence "  # noqa: S608
        "FROM message_ingestion_order AS ingestion "
        "JOIN messages ON messages.id = ingestion.message_id "
        "AND messages.chat_jid = ingestion.chat_jid "
        f"WHERE messages.timestamp = ? AND messages.chat_jid IN ({placeholders}) "
        "AND messages.is_from_me = 0",
        [cursor, *jids],
    )
    row = await result.fetchone()
    return f"{_SEQUENCE_CURSOR_PREFIX}{row['sequence']}" if row and row["sequence"] else cursor


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
    async with atomic_write() as db:
        cursor = await db.execute(
            "DELETE FROM messages WHERE sender = ? AND timestamp < ?",
            (sender, before_timestamp),
        )
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
