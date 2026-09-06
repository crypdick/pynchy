"""Atomic terminal lifecycle fence checks shared by local work-item settlement."""

from __future__ import annotations

from aiosqlite import Connection

from pynchy.conversation.api import (
    ConversationLifecycleFence,
)


async def lifecycle_fence_matches(
    database: Connection,
    fence: ConversationLifecycleFence,
) -> bool:
    """Return whether one claimed terminal delivery still owns local settlement."""
    cursor = await database.execute(
        """
        SELECT 1
        FROM routed_conversations AS conversation
        JOIN conversation_deliveries AS delivery
          ON delivery.conversation_id = conversation.id
        WHERE conversation.id = ?
          AND conversation.control_closed = 1
          AND conversation.control_state_revision IS ?
          AND delivery.provider = ?
          AND delivery.route = ?
          AND delivery.delivery_id = ?
          AND delivery.status = 'claimed'
          AND delivery.claim_id = ?
        """,
        (
            fence.conversation_id,
            fence.control_state_revision,
            fence.identity.provider,
            fence.identity.route,
            fence.identity.delivery_id,
            fence.claim_id,
        ),
    )
    return await cursor.fetchone() is not None
