"""Provider-neutral identities and lifecycle models for routed conversations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves model annotations at runtime.
    ChatJid,
    GroupFolder,
    SessionId,
)

ConversationId = NewType("ConversationId", str)
ConversationSubjectNamespace = NewType("ConversationSubjectNamespace", str)
ConversationSubjectKey = NewType("ConversationSubjectKey", str)
ExternalProvider = NewType("ExternalProvider", str)
ExternalRoute = NewType("ExternalRoute", str)
ExternalDeliveryId = NewType("ExternalDeliveryId", str)
ConversationClaimId = NewType("ConversationClaimId", str)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class ConversationSubject:
    """Immutable external subject that owns one durable conversation.

    ``namespace`` names the subject type, such as ``linear.issue``. ``key``
    carries the provider's immutable subject key. Neither value represents a
    delivery, a control thread, a scheduled task, or a mutable display title.
    """

    namespace: ConversationSubjectNamespace
    key: ConversationSubjectKey

    def __post_init__(self) -> None:
        _require_text(self.namespace, "Conversation subject namespace")
        _require_text(self.key, "Conversation subject key")


@dataclass(frozen=True, slots=True)
class ExternalDeliveryIdentity:
    """Identity of one authenticated provider delivery."""

    provider: ExternalProvider
    route: ExternalRoute
    delivery_id: ExternalDeliveryId

    def __post_init__(self) -> None:
        _require_text(self.provider, "External provider")
        _require_text(self.route, "External route")
        _require_text(self.delivery_id, "External delivery ID")


@dataclass(frozen=True, slots=True)
class ExternalDeliveryReceipt:
    """Provider-neutral evidence retained after authenticating one delivery."""

    identity: ExternalDeliveryIdentity
    payload_sha256: str
    received_at: str

    def __post_init__(self) -> None:
        _require_text(self.payload_sha256, "External delivery payload hash")
        _require_text(self.received_at, "External delivery received timestamp")


@dataclass(frozen=True, slots=True)
class Conversation:
    """Durable agent context for one immutable external subject."""

    id: ConversationId
    workspace: GroupFolder
    subject: ConversationSubject
    session_id: SessionId | None
    created_at: str
    updated_at: str


class ConversationDeliveryStatus(StrEnum):
    """Durable execution state for one conversation delivery."""

    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ConversationDelivery:
    """FIFO work item linking an authenticated delivery to its conversation."""

    sequence: int
    identity: ExternalDeliveryIdentity
    conversation_id: ConversationId
    status: ConversationDeliveryStatus
    received_at: str
    claim_id: ConversationClaimId | None = None
    claimed_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationDeliveryAdmission:
    """Idempotent result of linking a receipt to a stable conversation."""

    conversation: Conversation
    delivery: ConversationDelivery
    created: bool


class ControlSurface(StrEnum):
    """Human-facing channel for operating a routed conversation."""

    DISCORD = "discord"


@dataclass(frozen=True, slots=True)
class ConversationControlBinding:
    """Replaceable human-facing thread for one durable conversation."""

    conversation_id: ConversationId
    surface: ControlSurface
    parent_workspace: GroupFolder
    parent_jid: ChatJid
    thread_jid: ChatJid
    title: str
    updated_at: str
