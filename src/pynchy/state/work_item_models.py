"""Typed inputs and conflicts for durable Linear work-item state operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pynchy.types import (
    WorkItemExecution as RuntimeWorkItemExecution,
)
from pynchy.types import (
    WorkItemExecutionStatus as RuntimeWorkItemExecutionStatus,
)

# Dataclass constructors are runtime type-checked, so beartype must resolve these names here.
WorkItemExecution = RuntimeWorkItemExecution
WorkItemExecutionStatus = RuntimeWorkItemExecutionStatus


class WorkItemClaimConflictError(RuntimeError):
    """Raised when an active execution already owns a Linear issue."""

    def __init__(self, execution: WorkItemExecution) -> None:
        self.execution = execution
        super().__init__(
            f"{execution.linear_issue_identifier} is already claimed by execution {execution.id}"
        )


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
class WorkItemTransitionRequest:
    """A lifecycle operation that requires an external provider receipt."""

    execution: WorkItemExecution
    request_id: str
    operation: str
    target_status: str
    result_execution_status: WorkItemExecutionStatus
    evidence_refs: tuple[str, ...] = ()
    summary: str | None = None
    blocker: str | None = None
    handoff_to: str | None = None
