"""Provider-neutral identities and lifecycle models for routed conversations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pynchy.conversation_primitives import (  # noqa: F401 - public runtime re-exports preserve the established conversation surface.
    ConversationClaimId,
    ConversationDeliveryCompletion,
    ConversationId,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class ConversationSubject:
    """Immutable external subject that owns one durable conversation.

    ``namespace`` scopes the subject type by provider and tenant or route, such
    as ``linear:<tenant>:issue``. It must prevent identical provider keys from
    aliasing across tenants or routes. ``key`` carries the provider's immutable
    subject key. Neither value represents a delivery, control thread, scheduled
    task, or mutable display title.
    """

    namespace: ConversationSubjectNamespace
    key: ConversationSubjectKey

    def __post_init__(self) -> None:
        _require_text(self.namespace, "Conversation subject namespace")
        _require_text(self.key, "Conversation subject key")


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
    control_closed: bool = False
    control_state_revision: str | None = None


class ConversationDeliveryStatus(StrEnum):
    """Durable execution state for one conversation delivery."""

    HELD = "held"
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
    payload: dict[str, Any] | None = None
    claim_id: ConversationClaimId | None = None
    claimed_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationDeliveryAdmission:
    """Idempotent result of linking a receipt to a stable conversation."""

    conversation: Conversation
    delivery: ConversationDelivery
    created: bool
    terminal_retirement: TerminalConversationRetirement | None = None


@dataclass(frozen=True, slots=True)
class ConversationLifecycleFence:
    """Current terminal control and delivery claim allowed to settle local work."""

    conversation_id: ConversationId
    identity: ExternalDeliveryIdentity
    claim_id: ConversationClaimId
    control_state_revision: str | None


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
    closed: bool = False


@dataclass(frozen=True, slots=True)
class TerminalConversationRetirement:
    """Durable resources released while retaining a terminal lifecycle delivery."""

    runtime_folders: tuple[GroupFolder, ...]
    runtime_workspace_jids: tuple[ChatJid, ...]
    control_state_revision: str | None = None
    is_current: bool = True
