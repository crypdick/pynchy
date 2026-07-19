"""Owned state models for durable webhook admission receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves admission annotations at runtime.
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
    disposition: Literal["accepted", "ignored"]
    ignored_reason: str | None
    task_id: str | None
    occurred_at: str
    received_at: str

    def __post_init__(self) -> None:
        if (self.disposition == "accepted") != (self.task_id is not None):
            raise ValueError("Accepted webhook receipts require exactly one task")
        if (self.disposition == "ignored") != (self.ignored_reason is not None):
            raise ValueError("Ignored webhook receipts require a reason")


@dataclass(frozen=True)
class WebhookAdmission:
    """Idempotent admission result for one provider delivery."""

    receipt: WebhookReceipt
    task: ScheduledTask | None
    created: bool
