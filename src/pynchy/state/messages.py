"""Message storage and retrieval."""

from __future__ import annotations

import json
from typing import Any

from pynchy.conversation.phoenix import (
    ConversationBodyReader,  # noqa: TC001 - beartype resolves annotations.
)
from pynchy.state.connection import _get_db
from pynchy.state.conversation_events import (
    default_body_reader,
    get_conversation_event_pointers_since,
    hydrate_pointer_to_message,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves state API annotations.
    NewMessage,
)


async def store_message(msg: NewMessage, message_type: str = "user") -> None:
    """SQLite-only low-level row writer.

    Live conversation ingestion writes durable bodies through ``ConversationSink``
    and leaves only pointer projections in SQLite. This helper remains for
    non-conversation rows and narrow state tests.

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


async def store_message_direct(  # noqa: PLR0913, RUF100 - DB row writer keeps the message columns explicit.
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
    """SQLite-only low-level row writer with explicit fields.

    Do not use this for chat history. Conversation content belongs in Phoenix
    via ``ConversationSink``; SQLite stores the projection pointer.

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
    await db.commit()


async def message_exists(msg_id: str, chat_jid: str) -> bool:
    """Check if a message with the given ID and chat JID already exists."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT 1 FROM conversation_events
        WHERE source_message_id = ? AND chat_jid = ?
        LIMIT 1
        """,
        (msg_id, chat_jid),
    )
    return await cursor.fetchone() is not None


def _is_inbound_projection(row: dict[str, Any]) -> bool:
    return row["kind"] != "system_notice" and row["message_type"] not in {"assistant", "host"}


async def _hydrate_projected_messages_since(
    chat_jid: str,
    since_timestamp: str | None,
    *,
    body_reader: ConversationBodyReader | None = None,
    inbound_only: bool,
) -> list[NewMessage]:
    projected_rows = await get_conversation_event_pointers_since(chat_jid, since_timestamp)
    projected = []
    reader = body_reader
    for row in projected_rows:
        if inbound_only and not _is_inbound_projection(row):
            continue
        if reader is None:
            reader = default_body_reader()
        projected.append(await hydrate_pointer_to_message(row, reader))
    return projected


async def get_new_messages(jids: list[str], last_timestamp: str) -> tuple[list[NewMessage], str]:
    """Get inbound conversation projections across multiple groups since a timestamp."""
    if not jids:
        return [], last_timestamp

    messages = []
    reader = default_body_reader()
    for jid in jids:
        messages.extend(
            await _hydrate_projected_messages_since(
                jid,
                last_timestamp,
                body_reader=reader,
                inbound_only=True,
            )
        )

    messages.sort(key=lambda msg: (msg.timestamp, msg.id))
    new_timestamp = max((msg.timestamp for msg in messages), default=last_timestamp)
    return messages, new_timestamp


async def get_messages_since(
    chat_jid: str,
    since_timestamp: str | None,
    *,
    body_reader: ConversationBodyReader | None = None,
) -> list[NewMessage]:
    """Get inbound conversation projections for a specific chat since a timestamp."""
    projected = await _hydrate_projected_messages_since(
        chat_jid,
        since_timestamp,
        body_reader=body_reader,
        inbound_only=True,
    )
    return sorted(projected, key=lambda msg: (msg.timestamp, msg.id))


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
            (SELECT COUNT(*) FROM conversation_events
             WHERE kind != 'system_notice' AND message_type NOT IN ('assistant', 'host'))
                AS total_inbound,
            (SELECT COUNT(*) FROM outbound_ledger) AS total_outbound,
            (SELECT MAX(timestamp) FROM conversation_events
             WHERE kind != 'system_notice' AND message_type NOT IN ('assistant', 'host'))
                AS last_received_at,
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
    body_reader: ConversationBodyReader | None = None,
) -> list[NewMessage]:
    """Get recent messages for a chat, including bot responses. Newest last.

    Respects the cleared_at boundary — messages before it are hidden.
    """
    db = _get_db()
    cleared_cursor = await db.execute("SELECT cleared_at FROM chats WHERE jid = ?", (chat_jid,))
    cleared_row = await cleared_cursor.fetchone()
    cleared_at = cleared_row["cleared_at"] if cleared_row and cleared_row["cleared_at"] else None

    projected = await _hydrate_projected_messages_since(
        chat_jid,
        cleared_at,
        body_reader=body_reader,
        inbound_only=False,
    )

    merged = sorted(projected, key=lambda msg: (msg.timestamp, msg.id), reverse=True)
    return list(reversed(merged[:limit]))
