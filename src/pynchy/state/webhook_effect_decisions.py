"""Authoritative execution decisions for webhook-correlated deliveries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pynchy.conversation.api import (
    ConversationDeliveryStatus,
    ExternalDeliveryIdentity,
)

if TYPE_CHECKING:
    from aiosqlite import Connection
else:
    Connection = Any


async def webhook_effect_decision(
    database: Connection,
    identity: ExternalDeliveryIdentity,
) -> str | None:
    """Return the durable callback-correlation decision for one delivery."""
    cursor = await database.execute(
        """
        SELECT decision FROM webhook_effect_decisions
        WHERE provider = ? AND route = ? AND delivery_id = ?
        """,
        (identity.provider, identity.route, identity.delivery_id),
    )
    row = await cursor.fetchone()
    return row["decision"] if row is not None else None


async def set_webhook_effect_decision(
    database: Connection,
    identity: ExternalDeliveryIdentity,
    decision: str,
    *,
    reason: str | None = None,
) -> None:
    """Persist the current execution decision independently of the receipt audit."""
    await database.execute(
        """
        INSERT INTO webhook_effect_decisions (
            provider, route, delivery_id, decision, reason, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, route, delivery_id) DO UPDATE SET
            decision = excluded.decision,
            reason = excluded.reason,
            decided_at = excluded.decided_at
        """,
        (
            identity.provider,
            identity.route,
            identity.delivery_id,
            decision,
            reason,
            datetime.now(UTC).isoformat(),
        ),
    )


async def webhook_effect_delivery_status(
    database: Connection,
    identity: ExternalDeliveryIdentity,
) -> ConversationDeliveryStatus | None:
    """Return authoritative FIFO status, or no delivery when suppressed."""
    decision = await webhook_effect_decision(database, identity)
    if decision == "suppressed":
        return None
    if decision == "held":
        return ConversationDeliveryStatus.HELD
    return ConversationDeliveryStatus.PENDING
