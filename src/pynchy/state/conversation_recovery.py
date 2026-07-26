"""Startup repair for durable routed-conversation execution state."""

from __future__ import annotations

from aiosqlite import (  # noqa: TC002, RUF100 - beartype resolves recovery annotations at runtime.
    Connection,
)

from pynchy.state.connection import atomic_write
from pynchy.state.conversation_runtime_repair import (
    repair_conversation_runtime_ownership,
)

_RUNTIME_OWNER_MIGRATION_KEY = "migration:conversation_runtime_receipt_owner:v1"
_MIGRATION_COMPLETE = "complete"


async def _repair_conversation_runtime_ownership(database: Connection) -> int:
    """Run receipt-based owner repair once while retaining safe normalization."""
    cursor = await database.execute(
        "SELECT value FROM router_state WHERE key = ?",
        (_RUNTIME_OWNER_MIGRATION_KEY,),
    )
    marker = await cursor.fetchone()
    allow_owner_reassignment = marker is None or marker["value"] != _MIGRATION_COMPLETE
    migrated = await repair_conversation_runtime_ownership(
        database,
        allow_owner_reassignment=allow_owner_reassignment,
    )
    if allow_owner_reassignment:
        # This marker shares the repair transaction: a fail-closed ownership
        # conflict rolls back both, so the authenticated migration can retry.
        await database.execute(
            "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
            (_RUNTIME_OWNER_MIGRATION_KEY, _MIGRATION_COMPLETE),
        )
    return migrated


async def prepare_conversation_runtime_ownership_recovery() -> int:
    """Move thread-derived runtimes to conversation ownership before state load."""
    async with atomic_write() as database:
        return await _repair_conversation_runtime_ownership(database)


async def prepare_conversation_delivery_recovery() -> int:
    """Repair reset-hidden work, then release retryable orphaned claims.

    Reset ordering can commit a control-thread clear while retaining the
    conversation-owned session or the FIFO claim synchronously injected by the
    preceding turn's completion callback. The clear boundary distinguishes
    discarded work from post-reset deliveries.
    """
    async with atomic_write() as database:
        migrated_runtime_state = await _repair_conversation_runtime_ownership(database)
        await database.execute(
            """
            UPDATE routed_conversations
            SET session_id = NULL
            WHERE session_id IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM conversation_control_bindings AS binding
                  JOIN chats ON chats.jid = binding.thread_jid
                  WHERE binding.conversation_id = routed_conversations.id
                    AND chats.cleared_at IS NOT NULL
                    AND julianday(routed_conversations.updated_at)
                        <= julianday(chats.cleared_at)
              )
            """
        )
        retired = await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'completed', completed_at = (
                SELECT chats.cleared_at
                FROM conversation_control_bindings AS binding
                JOIN chats ON chats.jid = binding.thread_jid
                WHERE binding.conversation_id = conversation_deliveries.conversation_id
            )
            WHERE EXISTS (
                SELECT 1
                FROM conversation_control_bindings AS binding
                JOIN chats ON chats.jid = binding.thread_jid
                WHERE binding.conversation_id = conversation_deliveries.conversation_id
                  AND chats.cleared_at IS NOT NULL
                  AND julianday(conversation_deliveries.received_at)
                      <= julianday(chats.cleared_at)
            )
              AND (
                  status IN ('held', 'pending')
                  OR (
                      status = 'claimed'
                      AND NOT EXISTS (
                          SELECT 1 FROM in_flight_turns
                          WHERE in_flight_turns.conversation_claim_id =
                              conversation_deliveries.claim_id
                      )
                  )
              )
            """
        )
        await database.execute(
            """
            DELETE FROM webhook_effect_candidates
            WHERE EXISTS (
                SELECT 1 FROM conversation_deliveries
                WHERE conversation_deliveries.provider =
                          webhook_effect_candidates.provider
                  AND conversation_deliveries.route =
                          webhook_effect_candidates.route
                  AND conversation_deliveries.delivery_id =
                          webhook_effect_candidates.delivery_id
                  AND conversation_deliveries.status = 'completed'
            )
            """
        )
        released = await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'pending', claim_id = NULL, claimed_at = NULL
            WHERE status = 'claimed'
              AND NOT EXISTS (
                  SELECT 1 FROM in_flight_turns
                  WHERE in_flight_turns.conversation_claim_id = conversation_deliveries.claim_id
              )
            """
        )
        return migrated_runtime_state + retired.rowcount + released.rowcount
