"""Atomic recovery for unavailable provider sessions."""

from __future__ import annotations

from datetime import UTC, datetime

from aiosqlite import Connection

from pynchy.identifiers import (
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
        await _clear_runtime_session_reference(database, group_folder, session_id, chat_jid)


async def clear_runtime_session_references_batch(
    references: tuple[tuple[GroupFolder, SessionId, ChatJid], ...],
) -> None:
    """Atomically discard provider references for one policy publication."""
    async with atomic_write() as database:
        for group_folder, session_id, chat_jid in references:
            await _clear_runtime_session_reference(database, group_folder, session_id, chat_jid)


async def _clear_runtime_session_reference(
    database: Connection,
    group_folder: GroupFolder,
    session_id: SessionId,
    chat_jid: ChatJid,
) -> None:
    await database.execute(
        "DELETE FROM sessions WHERE group_folder = ? AND session_id = ?",
        (group_folder, session_id),
    )
    # Policy replacement is not a context reset. Keep sticky security taint so
    # a fresh gate cannot silently regain trust.
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
