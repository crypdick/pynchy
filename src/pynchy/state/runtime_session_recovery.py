"""Atomic recovery for unavailable provider sessions."""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.identifiers import (  # noqa: TC001, RUF100 - beartype resolves recovery annotations at runtime.
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.state.connection import atomic_write


async def clear_runtime_session_references(
    group_folder: GroupFolder,
    session_id: SessionId,
    chat_jid: ChatJid,
) -> None:
    """Discard one unavailable provider session from every durable owner."""
    async with atomic_write() as database:
        await database.execute(
            "DELETE FROM sessions WHERE group_folder = ? AND session_id = ?",
            (group_folder, session_id),
        )
        # Unavailability is not an authorized context reset. Keep sticky
        # security taint on the runtime so the replacement provider session
        # cannot silently regain trust on its next turn.
        await database.execute(
            """
            UPDATE routed_conversations
            SET session_id = NULL, updated_at = ?
            WHERE session_id = ?
              AND EXISTS (
                  SELECT 1
                  FROM conversation_control_bindings AS binding
                  WHERE binding.conversation_id = routed_conversations.id
                    AND binding.thread_jid = ?
              )
            """,
            (datetime.now(UTC).isoformat(), session_id, chat_jid),
        )
        await database.execute(
            """
            UPDATE in_flight_turns
            SET session_id = NULL
            WHERE group_folder = ? AND session_id = ?
            """,
            (group_folder, session_id),
        )
