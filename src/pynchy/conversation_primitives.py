"""Dependency-free identities shared by routed conversations and effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

ConversationId = NewType("ConversationId", str)
ConversationSubjectNamespace = NewType("ConversationSubjectNamespace", str)
ConversationSubjectKey = NewType("ConversationSubjectKey", str)
ExternalProvider = NewType("ExternalProvider", str)
ExternalRoute = NewType("ExternalRoute", str)
ExternalDeliveryId = NewType("ExternalDeliveryId", str)
ConversationClaimId = NewType("ConversationClaimId", str)


@dataclass(frozen=True, slots=True)
class ConversationDeliveryCompletion:
    """Identity needed to wake a completed delivery's pending sibling."""

    identity: ExternalDeliveryIdentity
    conversation_id: ConversationId


@dataclass(frozen=True, slots=True)
class ExternalDeliveryIdentity:
    """Identity of one authenticated provider delivery."""

    provider: ExternalProvider
    route: ExternalRoute
    delivery_id: ExternalDeliveryId

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("External provider must not be empty")
        if not self.route.strip():
            raise ValueError("External route must not be empty")
        if not self.delivery_id.strip():
            raise ValueError("External delivery ID must not be empty")
