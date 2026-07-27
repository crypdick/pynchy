"""One-time repairs for historical routed-conversation identities."""

from __future__ import annotations

from datetime import UTC, datetime

from aiosqlite import Connection  # noqa: TC002, RUF100 - beartype resolves annotations.


class ConversationIdentityMigrationConflictError(RuntimeError):
    """A duplicate conversation still owns work that cannot be retired safely."""


async def retire_duplicate_linear_conversations(database: Connection) -> int:
    """Retire older aliases created while Linear account namespaces changed."""
    duplicate_cursor = await database.execute(
        """
        SELECT workspace, subject_key
        FROM routed_conversations
        WHERE subject_namespace LIKE 'linear:%:issue'
        GROUP BY workspace, subject_key
        HAVING COUNT(*) > 1
        """
    )
    retired_count = 0
    for duplicate in await duplicate_cursor.fetchall():
        aliases_cursor = await database.execute(
            """
            SELECT conversation.id, conversation.subject_namespace,
                   conversation.session_id, conversation.created_at,
                   MAX(delivery.received_at) AS latest_delivery
            FROM routed_conversations AS conversation
            LEFT JOIN conversation_deliveries AS delivery
              ON delivery.conversation_id = conversation.id
            WHERE conversation.workspace = ?
              AND conversation.subject_key = ?
              AND conversation.subject_namespace LIKE 'linear:%:issue'
            GROUP BY conversation.id
            ORDER BY COALESCE(
                         MAX(delivery.received_at),
                         conversation.created_at
                     ) DESC,
                     conversation.created_at DESC,
                     conversation.id DESC
            """,
            (duplicate["workspace"], duplicate["subject_key"]),
        )
        aliases = list(await aliases_cursor.fetchall())
        for alias in aliases[1:]:
            await _assert_alias_is_retirable(database, alias["id"], alias["session_id"])
            await _retire_alias(database, alias["id"], alias["session_id"])
            retired_count += 1
    return retired_count


async def _assert_alias_is_retirable(
    database: Connection,
    conversation_id: str,
    session_id: str | None,
) -> None:
    cursor = await database.execute(
        """
        SELECT
            EXISTS (
                SELECT 1 FROM conversation_deliveries
                WHERE conversation_id = ? AND status != 'completed'
            ),
            EXISTS (
                SELECT 1 FROM scheduled_tasks
                WHERE conversation_id = ? AND status IN ('active', 'paused')
            ),
            EXISTS (
                SELECT 1 FROM in_flight_turns
                WHERE session_id = ?
            ),
            EXISTS (
                SELECT 1
                FROM webhook_effect_candidates AS candidate
                JOIN conversation_deliveries AS delivery
                  ON delivery.provider = candidate.provider
                 AND delivery.route = candidate.route
                 AND delivery.delivery_id = candidate.delivery_id
                WHERE delivery.conversation_id = ?
            ),
            EXISTS (
                SELECT 1 FROM routed_conversations
                WHERE session_id = ? AND id != ?
            )
        """,
        (
            conversation_id,
            conversation_id,
            session_id,
            conversation_id,
            session_id,
            conversation_id,
        ),
    )
    state = await cursor.fetchone()
    if state is not None and any(bool(value) for value in state):
        raise ConversationIdentityMigrationConflictError(
            f"Duplicate conversation {conversation_id} still owns active runtime state"
        )


async def _retire_alias(
    database: Connection,
    conversation_id: str,
    session_id: str | None,
) -> None:
    now = datetime.now(UTC).isoformat()
    await database.execute(
        """
        UPDATE routed_conversations
        SET subject_namespace = ?, session_id = NULL, updated_at = ?
        WHERE id = ?
        """,
        (f"retired:linear-conversation:{conversation_id}", now, conversation_id),
    )
    await database.execute(
        """
        UPDATE conversation_control_bindings
        SET closed = 1, updated_at = ?
        WHERE conversation_id = ?
        """,
        (now, conversation_id),
    )
    if session_id is not None:
        await database.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )
