"""Durable human-facing control bindings for routed conversations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiosqlite import Connection

from pynchy.conversation.api import (
    ControlSurface,
    ConversationClaimId,
    ConversationControlBinding,
    ConversationId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
    TerminalConversationRetirement,
)
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
)
from pynchy.state.connection import _get_db, atomic_write
from pynchy.state.conversation_terminal_runtime import terminal_runtime_resources

if TYPE_CHECKING:
    from aiosqlite import Row
else:
    Row = Any


class ConversationControlWorkspaceChangedError(RuntimeError):
    """A newer provider placement superseded this control reconciliation."""


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


def _normalized_control_state_revision(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Conversation control revision must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("Conversation control revision must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _provider_state_is_current(
    *,
    current_closed: bool,
    current_revision: str | None,
    observed_closed: bool,
    observed_revision: str | None,
) -> bool:
    if observed_revision is None:
        return current_revision is None
    if current_revision is None or observed_revision > current_revision:
        return True
    return observed_revision == current_revision and observed_closed is current_closed


async def apply_conversation_control_state(
    conversation_id: ConversationId,
    *,
    closed: bool,
    control_state_revision: str | None,
) -> bool:
    """Atomically apply a provider-observed control state if it is current.

    Unversioned provider observations may update controls without provider
    ordering, but cannot override a state ordered by a provider revision.
    The boolean result distinguishes an accepted current observation from stale input.
    """
    async with atomic_write() as database:
        return await _apply_conversation_control_state(
            database,
            conversation_id,
            closed=closed,
            control_state_revision=control_state_revision,
        )


async def _apply_conversation_control_state(
    database: Connection,
    conversation_id: ConversationId,
    *,
    closed: bool,
    control_state_revision: str | None,
) -> bool:
    """Apply provider control state inside the caller's write transaction."""
    expected_revision = _normalized_control_state_revision(control_state_revision)
    cursor = await database.execute(
        "SELECT control_closed, control_state_revision FROM routed_conversations WHERE id = ?",
        (conversation_id,),
    )
    conversation = await cursor.fetchone()
    if conversation is None:
        raise ValueError(f"Unknown conversation: {conversation_id}")
    current_closed = bool(conversation["control_closed"])
    current_revision = _normalized_control_state_revision(conversation["control_state_revision"])
    if not _provider_state_is_current(
        current_closed=current_closed,
        current_revision=current_revision,
        observed_closed=closed,
        observed_revision=expected_revision,
    ):
        return False

    effective_revision = expected_revision or current_revision
    if current_closed is closed and current_revision == effective_revision:
        return True
    now = datetime.now(UTC).isoformat()
    await database.execute(
        """
        UPDATE routed_conversations
        SET control_closed = ?, control_state_revision = ?, updated_at = ?
        WHERE id = ?
        """,
        (closed, effective_revision, now, conversation_id),
    )
    await database.execute(
        """
        UPDATE conversation_control_bindings
        SET closed = ?, updated_at = ?
        WHERE conversation_id = ? AND closed != ?
        """,
        (closed, now, conversation_id, closed),
    )
    return True


async def conversation_control_state_matches(
    conversation_id: ConversationId,
    *,
    closed: bool,
    control_state_revision: str | None,
    delivery_identity: ExternalDeliveryIdentity | None = None,
    claim_id: ConversationClaimId | None = None,
) -> bool:
    """Return whether a provider state and optional lifecycle claim remain current."""
    if (delivery_identity is None) != (claim_id is None):
        raise ValueError("Conversation control claim requires delivery identity and claim ID")
    expected_revision = _normalized_control_state_revision(control_state_revision)
    async with atomic_write() as database:
        cursor = await database.execute(
            """
            SELECT control_closed, control_state_revision
            FROM routed_conversations
            WHERE id = ?
            """,
            (conversation_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        current_revision = _normalized_control_state_revision(row["control_state_revision"])
        if bool(row["control_closed"]) is not closed or current_revision != expected_revision:
            return False
        if delivery_identity is None or claim_id is None:
            return True
        claim_cursor = await database.execute(
            """
            SELECT 1 FROM conversation_deliveries
            WHERE provider = ? AND route = ? AND delivery_id = ?
              AND conversation_id = ? AND status = 'claimed' AND claim_id = ?
            """,
            (
                delivery_identity.provider,
                delivery_identity.route,
                delivery_identity.delivery_id,
                conversation_id,
                claim_id,
            ),
        )
        return await claim_cursor.fetchone() is not None


async def retire_conversation_for_terminal(
    conversation_id: ConversationId,
    *,
    preserve_delivery: ExternalDeliveryIdentity | None,
    control_state_revision: str | None = None,
) -> TerminalConversationRetirement:
    """Terminally retire routed work, optionally retaining one lifecycle delivery."""
    async with atomic_write() as database:
        return await _retire_conversation_for_terminal(
            database,
            conversation_id,
            preserve_delivery=preserve_delivery,
            control_state_revision=control_state_revision,
        )


async def _retire_conversation_for_terminal(
    database: Connection,
    conversation_id: ConversationId,
    *,
    preserve_delivery: ExternalDeliveryIdentity | None,
    control_state_revision: str | None = None,
) -> TerminalConversationRetirement:
    """Retire durable conversation work inside the caller's write transaction."""
    now = datetime.now(UTC).isoformat()
    expected_revision = _normalized_control_state_revision(control_state_revision)
    cursor = await database.execute(
        """
        SELECT workspace, session_id, control_closed, control_state_revision
        FROM routed_conversations
        WHERE id = ?
        """,
        (conversation_id,),
    )
    conversation = await cursor.fetchone()
    if conversation is None:
        raise ValueError(f"Unknown conversation: {conversation_id}")
    binding_cursor = await database.execute(
        """
        SELECT parent_workspace, thread_jid
        FROM conversation_control_bindings
        WHERE conversation_id = ?
        """,
        (conversation_id,),
    )
    binding = await binding_cursor.fetchone()
    folders, workspace_jids = await terminal_runtime_resources(
        database,
        conversation_id,
        conversation["workspace"],
        binding,
    )
    current_revision = _normalized_control_state_revision(conversation["control_state_revision"])
    current_closed = bool(conversation["control_closed"])
    effective_revision = expected_revision or current_revision
    if not _provider_state_is_current(
        current_closed=current_closed,
        current_revision=current_revision,
        observed_closed=True,
        observed_revision=expected_revision,
    ):
        if preserve_delivery is not None:
            await database.execute(
                """
                UPDATE conversation_deliveries
                SET status = 'completed', claim_id = NULL, claimed_at = NULL, completed_at = ?
                WHERE provider = ? AND route = ? AND delivery_id = ?
                  AND status != 'completed'
                """,
                (
                    now,
                    preserve_delivery.provider,
                    preserve_delivery.route,
                    preserve_delivery.delivery_id,
                ),
            )
        return TerminalConversationRetirement(
            runtime_folders=tuple(sorted(folders)),
            runtime_workspace_jids=tuple(sorted(workspace_jids)),
            control_state_revision=current_revision,
            is_current=False,
        )

    if (
        not current_closed
        or conversation["session_id"] is not None
        or current_revision != effective_revision
    ):
        await database.execute(
            """
            UPDATE routed_conversations
            SET control_closed = 1, control_state_revision = ?, session_id = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (effective_revision, now, conversation_id),
        )
    await database.execute(
        """
        UPDATE conversation_control_bindings
        SET closed = 1, updated_at = ?
        WHERE conversation_id = ? AND closed = 0
        """,
        (now, conversation_id),
    )
    if preserve_delivery is None:
        await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'completed', claim_id = NULL, claimed_at = NULL, completed_at = ?
            WHERE conversation_id = ? AND status != 'completed'
            """,
            (now, conversation_id),
        )
    else:
        await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'completed', claim_id = NULL, claimed_at = NULL, completed_at = ?
            WHERE conversation_id = ? AND status != 'completed'
              AND NOT (provider = ? AND route = ? AND delivery_id = ?)
            """,
            (
                now,
                conversation_id,
                preserve_delivery.provider,
                preserve_delivery.route,
                preserve_delivery.delivery_id,
            ),
        )
    for folder in folders:
        await database.execute("DELETE FROM in_flight_turns WHERE group_folder = ?", (folder,))
        await database.execute("DELETE FROM sessions WHERE group_folder = ?", (folder,))
        await database.execute(
            "DELETE FROM session_security_taint WHERE group_folder = ?",
            (folder,),
        )
    return TerminalConversationRetirement(
        runtime_folders=tuple(sorted(folders)),
        runtime_workspace_jids=tuple(sorted(workspace_jids)),
        control_state_revision=effective_revision,
    )


async def set_conversation_control_binding(
    binding: ConversationControlBinding,
    *,
    owner_workspace: GroupFolder | None = None,
    expected_workspace: GroupFolder | None = None,
) -> ConversationControlBinding:
    """Atomically move a control and, only when explicit, its runtime owner."""
    async with atomic_write() as database:
        cursor = await database.execute(
            "SELECT workspace, control_closed FROM routed_conversations WHERE id = ?",
            (binding.conversation_id,),
        )
        conversation = await cursor.fetchone()
        if conversation is None:
            raise ValueError(f"Unknown conversation: {binding.conversation_id}")
        current_workspace = GroupFolder(conversation["workspace"])
        if expected_workspace is not None and current_workspace != expected_workspace:
            raise ConversationControlWorkspaceChangedError(
                f"Conversation workspace changed from {expected_workspace} to {current_workspace}"
            )
        # A stale scheduler must never overwrite a terminal lifecycle decision.
        if bool(conversation["control_closed"]):
            binding = replace(binding, closed=True)
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
