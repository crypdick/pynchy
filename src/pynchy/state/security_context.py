"""Bounded SQLite context for host security decisions."""

from __future__ import annotations

import json

from pynchy.security_context import (
    RecentSecurityContext,
    SecurityContextMessage,
    SecurityContextRole,
    SecurityExecutionAuthority,
    SecurityExecutionAuthorityKind,
)
from pynchy.state.connection import _get_db

_MESSAGE_LIMIT = 4
_MESSAGE_CHAR_LIMIT = 500
_TOOL_LIMIT = 8
_AGENT_UPDATE_LIMIT = 2


async def _load_execution_authority(chat_jid: str) -> SecurityExecutionAuthority | None:
    """Load authority only for the active, unfrozen occurrence in this chat."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT execution.linear_issue_identifier
        FROM in_flight_turns AS turn
        JOIN scheduled_tasks AS task ON task.id = turn.task_id
        JOIN work_item_executions AS execution ON execution.task_id = turn.task_id
        WHERE turn.chat_jid = ?
          AND turn.work_kind = 'scheduled'
          AND turn.control_state = 'active'
          AND task.status = 'active'
          AND execution.status = 'in_progress'
        ORDER BY turn.started_at DESC, execution.updated_at DESC
        LIMIT 1
        """,
        (chat_jid,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return SecurityExecutionAuthority(
        kind=SecurityExecutionAuthorityKind.LINEAR_WORK_ITEM_LEASE,
        work_item_identifier=str(row["linear_issue_identifier"])[:100],
    )


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
        execution_authority=await _load_execution_authority(chat_jid),
    )
