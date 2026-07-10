"""Conversation event projection storage."""

from __future__ import annotations

import json
from typing import Any

from pynchy.config import get_settings
from pynchy.conversation.endpoints import _normalized_endpoint, resolved_phoenix_endpoint
from pynchy.conversation.events import (
    ConversationEvent,  # noqa: TC001 - beartype resolves annotations.
)
from pynchy.conversation.phoenix import (
    ConversationBodyReader,
    PhoenixConversationBodyReader,
    PhoenixEventRef,
)
from pynchy.state.connection import _get_db
from pynchy.types import NewMessage


class ConversationProjectionHydrationError(RuntimeError):
    """Raised when a projected conversation row cannot be hydrated from Phoenix."""


def _metadata_json(event: ConversationEvent) -> str:
    metadata_json = event.span_attributes().get("pynchy.metadata_json")
    if isinstance(metadata_json, str):
        return metadata_json
    return "{}"


async def store_conversation_event_pointer(
    event: ConversationEvent,
    ref: PhoenixEventRef,
) -> None:
    if ref.event_id != event.event_id:
        raise ValueError(
            f"Phoenix ref event_id {ref.event_id!r} does not match event {event.event_id!r}"
        )

    db = _get_db()
    await db.execute(
        """
        INSERT OR IGNORE INTO conversation_events (
            event_id, turn_id, chat_jid, timestamp, kind, sender, sender_name,
            message_type, source_message_id, content_preview, phoenix_ref, metadata
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
    await db.commit()


def _decode_metadata(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def default_body_reader() -> ConversationBodyReader:
    settings = get_settings().conversation_store
    endpoint = _normalized_endpoint(settings.phoenix_endpoint) or resolved_phoenix_endpoint()
    return PhoenixConversationBodyReader(
        project_name=settings.project_name,
        endpoint=endpoint,
    )


def pointer_to_message(row: dict[str, Any], content: str) -> NewMessage:
    metadata = dict(row.get("metadata") or {})
    metadata["phoenix_ref"] = row["phoenix_ref"]
    metadata["turn_id"] = row["turn_id"]
    metadata["source_message_id"] = row["source_message_id"]
    return NewMessage(
        id=row["event_id"],
        chat_jid=row["chat_jid"],
        sender=row["sender"],
        sender_name=row["sender_name"],
        content=content,
        timestamp=row["timestamp"],
        is_from_me=row["message_type"] in {"assistant", "host"},
        message_type=row["message_type"],
        metadata=metadata,
    )


async def hydrate_pointer_to_message(
    row: dict[str, Any],
    body_reader: ConversationBodyReader | None = None,
) -> NewMessage:
    reader = body_reader or default_body_reader()
    try:
        content = await reader.read_event_content(row["event_id"])
    except Exception as exc:
        raise ConversationProjectionHydrationError(
            f"Failed to hydrate projected conversation event {row['event_id']} from Phoenix"
        ) from exc
    return pointer_to_message(row, content)


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
