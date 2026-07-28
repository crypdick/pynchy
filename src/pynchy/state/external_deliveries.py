"""Provider-neutral authenticated delivery receipts."""

from __future__ import annotations

from pynchy.conversation.api import (  # noqa: TC001, RUF100 - beartype resolves state annotations at runtime.
    ExternalDeliveryIdentity,
    ExternalDeliveryReceipt,
)
from pynchy.state.connection import _get_db, atomic_write


async def get_external_delivery_receipt(
    identity: ExternalDeliveryIdentity,
) -> ExternalDeliveryReceipt | None:
    """Return durable authentication evidence for one external delivery."""
    cursor = await _get_db().execute(
        """
        SELECT payload_sha256, received_at FROM external_receipts
        WHERE provider = ? AND route = ? AND delivery_id = ?
        """,
        (identity.provider, identity.route, identity.delivery_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return ExternalDeliveryReceipt(
        identity=identity,
        payload_sha256=row["payload_sha256"],
        received_at=row["received_at"],
    )


async def admit_external_delivery_receipt(receipt: ExternalDeliveryReceipt) -> bool:
    """Persist authenticated delivery evidence once.

    Returns ``True`` for a previously unseen receipt and ``False`` for an exact replay. The
    authentication adapter must reject untrusted input before calling this
    boundary.
    """
    async with atomic_write() as database:
        cursor = await database.execute(
            """
            SELECT payload_sha256, received_at FROM external_receipts
            WHERE provider = ? AND route = ? AND delivery_id = ?
            """,
            (
                receipt.identity.provider,
                receipt.identity.route,
                receipt.identity.delivery_id,
            ),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            if existing["payload_sha256"] != receipt.payload_sha256:
                raise ValueError("External delivery identity has conflicting receipt evidence")
            return False

        await database.execute(
            """
            INSERT INTO external_receipts (
                provider, route, delivery_id, payload_sha256, received_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                receipt.identity.provider,
                receipt.identity.route,
                receipt.identity.delivery_id,
                receipt.payload_sha256,
                receipt.received_at,
            ),
        )
        return True
