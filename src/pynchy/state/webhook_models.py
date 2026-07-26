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
class LinearCommentSelfEcho:
    """Exact provider evidence for one Pynchy-created Linear comment."""

    account_name: str
    comment_id: str
    issue_id: str
    revision: str
    action: str = "create"

    def __post_init__(self) -> None:
        if not all((self.account_name, self.comment_id, self.issue_id, self.revision, self.action)):
            raise ValueError("Linear self-echo marker fields cannot be blank")


@dataclass(frozen=True)
class LinearIssueStateSelfEcho:
    """Exact provider evidence for one Pynchy-created nonterminal state update."""

    account_name: str
    issue_id: str
    state_id: str
    revision: str
    action: str = "update"

    def __post_init__(self) -> None:
        if not all((self.account_name, self.issue_id, self.state_id, self.revision, self.action)):
            raise ValueError("Linear self-echo marker fields cannot be blank")


@dataclass(frozen=True)
class WebhookAdmission:
    """Idempotent admission result for one provider delivery."""

    receipt: WebhookReceipt
    task: ScheduledTask | None
    created: bool
    self_echo_suppressed: bool = False
