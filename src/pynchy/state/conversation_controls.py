"""Durable human-facing control bindings for routed conversations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationId,
    ExternalProvider,
    ExternalRoute,
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
        closed=bool(row["closed"]),
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


async def get_conversation_control_by_thread(
    thread_jid: ChatJid,
) -> ConversationControlBinding | None:
    """Return the current binding that owns one operator thread."""
    database = _get_db()
    cursor = await database.execute(
        """
        SELECT * FROM conversation_control_bindings
        WHERE surface = ? AND thread_jid = ?
        """,
        (ControlSurface.DISCORD.value, thread_jid),
    )
    row = await cursor.fetchone()
    return _row_to_binding(row) if row is not None else None


async def list_idle_conversation_ids(
    provider: ExternalProvider,
    route: ExternalRoute,
) -> tuple[ConversationId, ...]:
    """Return route conversations whose control state can be safely reconciled."""
    cursor = await _get_db().execute(
        """
        SELECT DISTINCT owned.conversation_id
        FROM conversation_deliveries AS owned
        WHERE owned.provider = ? AND owned.route = ?
          AND NOT EXISTS (
              SELECT 1 FROM conversation_deliveries AS active
              WHERE active.conversation_id = owned.conversation_id
                AND active.status = 'claimed'
          )
        ORDER BY owned.conversation_id
        """,
        (provider, route),
    )
    return tuple(ConversationId(row["conversation_id"]) for row in await cursor.fetchall())


async def close_conversation_control(conversation_id: ConversationId) -> bool:
    """Close an existing control without changing its conversation placement.

    Lifecycle deliveries may be retried after a process interruption.  The
    conditional update gives the close transition exactly-once durable state
    semantics while leaving control presentation and workspace ownership intact.
    """
    async with atomic_write() as database:
        cursor = await database.execute(
            """
            UPDATE conversation_control_bindings
            SET closed = 1, updated_at = ?
            WHERE conversation_id = ? AND closed = 0
            """,
            (datetime.now(UTC).isoformat(), conversation_id),
        )
        return cursor.rowcount == 1


async def set_conversation_control_binding(
    binding: ConversationControlBinding,
    *,
    owner_workspace: GroupFolder | None = None,
) -> ConversationControlBinding:
    """Atomically move a control and, only when explicit, its runtime owner."""
    async with atomic_write() as database:
        cursor = await database.execute(
            "SELECT 1 FROM routed_conversations WHERE id = ?",
            (binding.conversation_id,),
        )
        if await cursor.fetchone() is None:
            raise ValueError(f"Unknown conversation: {binding.conversation_id}")
        if owner_workspace is not None:
            await database.execute(
                "UPDATE routed_conversations SET workspace = ?, updated_at = ? WHERE id = ?",
                (owner_workspace, binding.updated_at, binding.conversation_id),
            )
        await database.execute(
            """
            INSERT INTO conversation_control_bindings (
                conversation_id, surface, parent_workspace, parent_jid,
                thread_jid, title, closed, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                surface = excluded.surface,
                parent_workspace = excluded.parent_workspace,
                parent_jid = excluded.parent_jid,
                thread_jid = excluded.thread_jid,
                title = excluded.title,
                closed = excluded.closed,
                updated_at = excluded.updated_at
            """,
            (
                binding.conversation_id,
                binding.surface.value,
                binding.parent_workspace,
                binding.parent_jid,
                binding.thread_jid,
                binding.title,
                binding.closed,
                binding.updated_at,
            ),
        )
    return binding
