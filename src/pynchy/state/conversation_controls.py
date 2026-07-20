"""Durable human-facing control bindings for routed conversations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationId,
)
from pynchy.state.connection import _get_db, atomic_write
from pynchy.types import ChatJid, GroupFolder

if TYPE_CHECKING:
    from aiosqlite import Row
else:
    Row = Any


def _row_to_binding(row: Row) -> ConversationControlBinding:
    return ConversationControlBinding(
        conversation_id=ConversationId(row["conversation_id"]),
        surface=ControlSurface(row["surface"]),
        parent_workspace=GroupFolder(row["parent_workspace"]),
        parent_jid=ChatJid(row["parent_jid"]),
        thread_jid=ChatJid(row["thread_jid"]),
        title=row["title"],
        updated_at=row["updated_at"],
    )


async def get_conversation_control_binding(
    conversation_id: ConversationId,
) -> ConversationControlBinding | None:
    """Return the current human-facing control thread for a conversation."""
    database = _get_db()
    cursor = await database.execute(
        "SELECT * FROM conversation_control_bindings WHERE conversation_id = ?",
        (conversation_id,),
    )
    row = await cursor.fetchone()
    return _row_to_binding(row) if row is not None else None


async def set_conversation_control_binding(
    binding: ConversationControlBinding,
) -> ConversationControlBinding:
    """Atomically move placement and set presentation without changing identity."""
    async with atomic_write() as database:
        cursor = await database.execute(
            "SELECT 1 FROM routed_conversations WHERE id = ?",
            (binding.conversation_id,),
        )
        if await cursor.fetchone() is None:
            raise ValueError(f"Unknown conversation: {binding.conversation_id}")
        await database.execute(
            "UPDATE routed_conversations SET workspace = ?, updated_at = ? WHERE id = ?",
            (binding.parent_workspace, binding.updated_at, binding.conversation_id),
        )
        await database.execute(
            """
            INSERT INTO conversation_control_bindings (
                conversation_id, surface, parent_workspace, parent_jid,
                thread_jid, title, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                surface = excluded.surface,
                parent_workspace = excluded.parent_workspace,
                parent_jid = excluded.parent_jid,
                thread_jid = excluded.thread_jid,
                title = excluded.title,
                updated_at = excluded.updated_at
            """,
            (
                binding.conversation_id,
                binding.surface.value,
                binding.parent_workspace,
                binding.parent_jid,
                binding.thread_jid,
                binding.title,
                binding.updated_at,
            ),
        )
    return binding
