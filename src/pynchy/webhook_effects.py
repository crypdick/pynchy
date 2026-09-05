"""Provider-neutral correlation models for outbound effects and webhook callbacks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

from pynchy.conversation_primitives import (  # noqa: TC001 - beartype resolves effect results.
    ConversationDeliveryCompletion,
)

WebhookEffectId = NewType("WebhookEffectId", str)


class WebhookEffectStatus(StrEnum):
    """Durable lifecycle for one outbound mutation and its callback evidence."""

    PREPARED = "prepared"  # noqa: V107
    EXECUTING = "executing"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECONCILED_ABSENT = "reconciled_absent"


class WebhookEffectCallbackDecision(StrEnum):
    """Pre-admission relationship between a callback and outbound effects."""

    UNRELATED = "unrelated"
    HELD = "held"
    SUPPRESSED = "suppressed"


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class WebhookEffectScope:
    """Coarse identity known before an outbound provider mutation starts."""

    provider: str
    account: str
    event_type: str
    event_action: str
    subject_id: str | None
    intent_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.provider, "Webhook effect provider")
        _require_text(self.account, "Webhook effect account")
        _require_text(self.event_type, "Webhook effect event type")
        _require_text(self.event_action, "Webhook effect event action")
        if self.subject_id is not None:
            _require_text(self.subject_id, "Webhook effect subject")
        if self.intent_fingerprint is not None:
            _require_text(self.intent_fingerprint, "Webhook effect intent fingerprint")


@dataclass(frozen=True, slots=True)
class WebhookEffectEvidence:
    """Exact opaque provider evidence shared by a response and its callback."""

    scope: WebhookEffectScope
    fingerprint: str

    def __post_init__(self) -> None:
        _require_text(self.fingerprint, "Webhook effect fingerprint")
        if self.scope.subject_id is None:
            raise ValueError("Exact webhook effect evidence requires a subject")


@dataclass(frozen=True, slots=True)
class WebhookEffectResolution:
    """Conversation deliveries whose FIFO may advance after resolving an effect."""

    wakeups: tuple[ConversationDeliveryCompletion, ...] = ()


@dataclass(frozen=True, slots=True)
class WebhookEffect:
    """Operator-visible projection of one durable outbound effect."""

    id: WebhookEffectId
    scope: WebhookEffectScope
    status: WebhookEffectStatus
    fingerprint: str | None
    created_at: str
    executing_at: str | None
    resolved_at: str | None
