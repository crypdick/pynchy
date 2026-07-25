"""Bounded SQLite context for host security decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from pynchy.state.connection import _get_db

_MESSAGE_LIMIT = 4
_MESSAGE_CHAR_LIMIT = 500
_TOOL_LIMIT = 8
_AGENT_UPDATE_LIMIT = 2


class SecurityContextRole(StrEnum):
    """Message roles exposed to the Cop context boundary."""

    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class SecurityContextMessage:
    """One bounded recent message without sender identifiers."""

    role: SecurityContextRole
    content: str


@dataclass(frozen=True)
class RecentSecurityContext:
    """The available intent and action chain for one chat."""

    current_user_intent: str | None
    recent_messages: tuple[SecurityContextMessage, ...]
    recent_agent_updates: tuple[str, ...]
    completed_tool_actions: tuple[str, ...]


async def load_recent_security_context(chat_jid: str) -> RecentSecurityContext:
    """Load a small context window without exposing tool inputs or full history."""
    db = _get_db()
    intent_cursor = await db.execute(
        """
        SELECT content FROM messages
        WHERE chat_jid = ? AND is_from_me = 0 AND message_type = 'user'
        ORDER BY timestamp DESC LIMIT 1
        """,
        (chat_jid,),
    )
    intent_row = await intent_cursor.fetchone()
    current_intent = (
        str(intent_row["content"])[:_MESSAGE_CHAR_LIMIT] if intent_row is not None else None
    )

    message_cursor = await db.execute(
        """
        SELECT content, is_from_me FROM messages
        WHERE chat_jid = ? AND message_type IN ('user', 'assistant')
        ORDER BY timestamp DESC LIMIT ?
        """,
        (chat_jid, _MESSAGE_LIMIT),
    )
    message_rows = list(await message_cursor.fetchall())
    message_rows.reverse()
    messages = tuple(
        SecurityContextMessage(
            role=(
                SecurityContextRole.ASSISTANT
                if bool(row["is_from_me"])
                else SecurityContextRole.USER
            ),
            content=str(row["content"])[:_MESSAGE_CHAR_LIMIT],
        )
        for row in message_rows
    )

    update_cursor = await db.execute(
        """
        SELECT payload FROM events
        WHERE chat_jid = ? AND event_type = 'agent_trace'
          AND json_extract(payload, '$.trace_type') = 'text'
        ORDER BY id DESC LIMIT ?
        """,
        (chat_jid, _AGENT_UPDATE_LIMIT),
    )
    updates: list[str] = []
    update_rows = list(await update_cursor.fetchall())
    update_rows.reverse()
    for row in update_rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        content = payload.get("content")
        if isinstance(content, str) and content:
            updates.append(content[:_MESSAGE_CHAR_LIMIT])

    event_cursor = await db.execute(
        """
        SELECT payload FROM events
        WHERE chat_jid = ? AND event_type = 'agent_trace'
          AND json_extract(payload, '$.trace_type') = 'tool_use'
        ORDER BY id DESC LIMIT ?
        """,
        (chat_jid, _TOOL_LIMIT),
    )
    tools: list[str] = []
    event_rows = list(await event_cursor.fetchall())
    event_rows.reverse()
    for row in event_rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        if payload.get("trace_type") != "tool_use":
            continue
        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            tools.append(tool_name[:100])

    return RecentSecurityContext(
        current_user_intent=current_intent,
        recent_messages=messages,
        recent_agent_updates=tuple(updates),
        completed_tool_actions=tuple(tools),
    )
