"""Typed inputs and conflicts for durable Linear work-item state operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pynchy.work_items.api import WorkItemClaimConflictError as RuntimeWorkItemClaimConflictError
from pynchy.work_items.api import WorkItemExecution as RuntimeWorkItemExecution
from pynchy.work_items.api import WorkItemExecutionStatus as RuntimeWorkItemExecutionStatus
from pynchy.work_items.api import WorkItemTransition as RuntimeWorkItemTransition
from pynchy.work_items.api import WorkItemTransitionRequest as RuntimeWorkItemTransitionRequest
from pynchy.work_items.api import WorkItemTransitionStatus as RuntimeWorkItemTransitionStatus

# Dataclass constructors are runtime type-checked, so beartype must resolve these names here.
WorkItemExecution = RuntimeWorkItemExecution
WorkItemExecutionStatus = RuntimeWorkItemExecutionStatus
WorkItemClaimConflictError = RuntimeWorkItemClaimConflictError
WorkItemTransition = RuntimeWorkItemTransition
WorkItemTransitionRequest = RuntimeWorkItemTransitionRequest
WorkItemTransitionStatus = RuntimeWorkItemTransitionStatus


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
