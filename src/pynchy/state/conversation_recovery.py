"""Startup repair for durable routed-conversation execution state."""

from __future__ import annotations

from pynchy.state.connection import atomic_write


async def prepare_conversation_delivery_recovery() -> int:
    """Repair reset-hidden work, then release retryable orphaned claims.

    Reset ordering can commit a control-thread clear while retaining the
    conversation-owned session or the FIFO claim synchronously injected by the
    preceding turn's completion callback. The clear boundary distinguishes
    discarded work from post-reset deliveries.
    """
    async with atomic_write() as database:
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
        return retired.rowcount + released.rowcount
