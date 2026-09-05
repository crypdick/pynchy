"""Provider-neutral execution and transition evidence for durable work items."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class WorkItemExecutionStatus(StrEnum):
    """Pynchy's durable lifecycle for one linked external work item."""

    CLAIMING = "claiming"
    IN_PROGRESS = "in_progress"
    AWAITING_REVIEW = "awaiting_review"
    FOLLOW_UPS = "follow_ups"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    HANDED_OFF = "handed_off"
    FAILED = "failed"

    @property
    def is_active(self) -> bool:
        return self in {self.CLAIMING, self.IN_PROGRESS, self.UNKNOWN}

    @property
    def is_explicit_lifecycle_outcome(self) -> bool:
        return not self.is_active and self is not self.FAILED


@dataclass(frozen=True)
class WorkItemExecution:
    """Durable link between an external item and one Pynchy execution attempt."""

    id: str
    workspace: str
    linear_issue_id: str
    linear_issue_identifier: str
    linear_issue_url: str
    turn_id: str | None
    task_id: str | None
    attempt: int
    flow_id: str | None
    temporal_workflow_id: str | None
    initiated_by: str
    observed_state_id: str
    observed_state_name: str
    observed_updated_at: str | None
    status: WorkItemExecutionStatus
    summary: str | None
    blocker: str | None
    handoff_to: str | None
    evidence_refs: tuple[str, ...]
    requester_delivery_status: str
    requester_delivery_turn_id: str | None
    requester_delivery_error: str | None
    requester_delivered_at: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


class WorkItemTransitionStatus(StrEnum):
    """Evidence status for a provider transition requested by an execution."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class WorkItemClaimConflictError(RuntimeError):
    """Raised when an active execution already owns an external work item."""

    def __init__(self, execution: WorkItemExecution) -> None:
        self.execution = execution
        super().__init__(
            f"{execution.linear_issue_identifier} is already claimed by execution {execution.id}"
        )


@dataclass(frozen=True)
class WorkItemTransition:
    """Intended external state change and its provider receipt or uncertainty."""

    id: int
    execution_id: str
    request_id: str
    operation: str
    target_status: str
    result_execution_status: WorkItemExecutionStatus
    evidence_refs: tuple[str, ...]
    summary: str | None
    blocker: str | None
    handoff_to: str | None
    status: WorkItemTransitionStatus
    receipt: dict[str, Any] | None
    error: str | None
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class WorkItemTransitionRequest:
    """A lifecycle operation that requires an external provider receipt."""

    execution: WorkItemExecution
    request_id: str
    operation: str
    target_status: str
    result_execution_status: WorkItemExecutionStatus
    evidence_refs: tuple[str, ...] | None = None
    summary: str | None = None
    blocker: str | None = None
    handoff_to: str | None = None
    requester_delivery_turn_id: str | None = None


@dataclass(frozen=True)
class WorkItemClaimRequest:
    """Host-derived provenance and observed Linear state for a claim."""

    workspace: str
    issue: dict[str, Any]
    turn_id: str | None
    task_id: str | None
    initiated_by: str
    request_id: str


@dataclass(frozen=True)
class WorkItemTransitionResolution:
    """Provider receipt and intended local terminal outcome for one transition."""

    transition: WorkItemTransition
    execution_status: WorkItemExecutionStatus
    transition_status: WorkItemTransitionStatus
    issue: dict[str, Any] | None = None
    error: str | None = None
