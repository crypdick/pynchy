"""Eligibility rules for untrusted Matrix timeline events."""

from __future__ import annotations

from pynchy.plugins.integrations.matrix_gateway_client import (
    MatrixSyncEvent,
)


def eligible_matrix_event(event: MatrixSyncEvent, *, owner_user_id: str) -> bool:
    """Accept decryptable live standalone text written by another user."""
    return bool(
        event.live
        and event.decrypted
        and event.event_type == "m.room.message"
        and event.message_type == "m.text"
        and event.body is not None
        and event.body.strip()
        and event.sender != owner_user_id
        and event.relation_type is None
        and not event.redacted
    )
