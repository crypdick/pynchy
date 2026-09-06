"""Conversation event projection pointer storage.

SQLite ``messages`` rows are the authoritative chat history. This module only
stores and reads projection pointer rows for database inspection; it has no
chat-history hydration path and imports no external trace-store clients.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pynchy.conversation.api import (
    ConversationEvent,
)
from pynchy.state.connection import _get_db, atomic_write


@dataclass(frozen=True, slots=True)
class ConversationEventRef:
    event_id: str
    trace_ref: str


def _metadata_json(event: ConversationEvent) -> str:
    metadata_json = event.span_attributes().get("pynchy.metadata_json")
    if isinstance(metadata_json, str):
        return metadata_json
    return "{}"


async def store_conversation_event_pointer(
    event: ConversationEvent,
    ref: ConversationEventRef,
) -> None:
    if ref.event_id != event.event_id:
        raise ValueError(
            f"Conversation ref event_id {ref.event_id!r} does not match event {event.event_id!r}"
        )

    async with atomic_write() as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO conversation_events (
                event_id, turn_id, chat_jid, timestamp, kind, sender, sender_name,
                message_type, source_message_id, content_preview, trace_ref, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.turn_id,
                event.chat_jid,
                event.timestamp,
                event.kind.value,
                event.sender,
                event.sender_name,
                event.message_type,
                event.source_message_id,
                event.preview,
                ref.trace_ref,
                _metadata_json(event),
            ),
        )


def _decode_metadata(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


async def get_conversation_event_pointers_since(
    chat_jid: str,
    since_timestamp: str | None,
) -> list[dict[str, Any]]:
    query = """
        SELECT * FROM conversation_events
        WHERE chat_jid = ?
    """
    params: list[object] = [chat_jid]
    if since_timestamp is not None:
        query += " AND timestamp > ?"
        params.append(since_timestamp)
    query += " ORDER BY timestamp ASC, event_id ASC"

    db = _get_db()
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        row["metadata"] = _decode_metadata(row.get("metadata"))
    return result
