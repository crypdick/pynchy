"""Transactional public admission boundary for conversation deliveries."""

from __future__ import annotations

from typing import Any

from pynchy.conversation.api import (  # noqa: TC001, RUF100 - beartype resolves public state annotations at runtime.
    ConversationDeliveryAdmission,
    ConversationSubject,
    ExternalDeliveryIdentity,
)
from pynchy.state.connection import atomic_write
from pynchy.state.conversation_routing import _admit_conversation_delivery
from pynchy.types import GroupFolder  # noqa: TC001, RUF100 - beartype resolves at runtime.


async def admit_conversation_delivery(
    identity: ExternalDeliveryIdentity,
    subject: ConversationSubject,
    workspace: GroupFolder,
    *,
    payload: dict[str, Any] | None = None,
) -> ConversationDeliveryAdmission | None:
    """Link one authenticated receipt to a conversation exactly once."""
    async with atomic_write() as database:
        return await _admit_conversation_delivery(
            database,
            identity,
            subject,
            workspace,
            payload=payload,
        )
