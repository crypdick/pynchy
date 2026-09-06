"""Owned state models for durable webhook admission receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pynchy.conversation.api import (
    ConversationDeliveryAdmission,
    ConversationSubject,
    ExternalDeliveryIdentity,
)
from pynchy.identifiers import (
    GroupFolder,
)
from pynchy.scheduling.api import (
    ScheduledTask,
)


@dataclass(frozen=True)
class WebhookReceipt:
    """Metadata retained after authenticating one external delivery."""

    provider: str
    route: str
    delivery_id: str
    workspace: str
    event_type: str
    event_action: str
    subject_id: str
    payload_sha256: str
    disposition: Literal["accepted", "routed", "lifecycle", "notified", "ignored"]
    ignored_reason: str | None
    task_id: str | None
    occurred_at: str
    received_at: str

    def __post_init__(self) -> None:
        if (self.disposition == "accepted") != (self.task_id is not None):
            raise ValueError("Accepted webhook receipts require exactly one task")
        if self.disposition == "routed" and (
            self.task_id is not None or self.ignored_reason is not None
        ):
            raise ValueError("Routed webhook receipts cannot create separate tasks")
        if self.disposition == "lifecycle" and (
            self.task_id is not None or self.ignored_reason is not None
        ):
            raise ValueError("Lifecycle webhook receipts cannot create separate tasks")
        if self.disposition == "notified" and (
            self.task_id is not None or self.ignored_reason is not None
        ):
            raise ValueError(
                "Notified webhook receipts cannot create tasks or carry ignore reasons"
            )
        if (self.disposition == "ignored") != (self.ignored_reason is not None):
            raise ValueError("Ignored webhook receipts require a reason")


@dataclass(frozen=True)
class WebhookAdmission:
    """Idempotent admission result for one provider delivery."""

    receipt: WebhookReceipt
    task: ScheduledTask | None
    created: bool
    outbound_effect_suppressed: bool = False
    outbound_effect_held: bool = False


@dataclass(frozen=True)
class WebhookConversationAdmission:
    """One transaction's immutable receipt and optional routed FIFO entry."""

    webhook: WebhookAdmission
    conversation: ConversationDeliveryAdmission | None


@dataclass(frozen=True)
class WebhookConversationRequest:
    """Parsed provider-neutral delivery data committed beside its receipt."""

    identity: ExternalDeliveryIdentity
    subject: ConversationSubject
    workspace: GroupFolder
    payload: dict[str, object]
    control_closed: bool | None = None
    control_state_revision: str | None = None
