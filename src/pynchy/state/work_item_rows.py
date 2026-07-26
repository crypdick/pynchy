"""SQLite-row projections for durable Linear work-item records."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiosqlite import Row
else:
    Row = Any

from pynchy.types import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransition,
    WorkItemTransitionStatus,
)


def row_to_execution(row: Row) -> WorkItemExecution:
    """Decode one work-item execution row into its typed domain record."""
    return WorkItemExecution(
        id=row["id"],
        workspace=row["workspace"],
        linear_issue_id=row["linear_issue_id"],
        linear_issue_identifier=row["linear_issue_identifier"],
        linear_issue_url=row["linear_issue_url"],
        turn_id=row["turn_id"],
        task_id=row["task_id"],
        attempt=row["attempt"],
        flow_id=row["flow_id"],
        temporal_workflow_id=row["temporal_workflow_id"],
        initiated_by=row["initiated_by"],
        observed_state_id=row["observed_state_id"],
        observed_state_name=row["observed_state_name"],
        observed_updated_at=row["observed_updated_at"],
        status=WorkItemExecutionStatus(row["status"]),
        summary=row["summary"],
        blocker=row["blocker"],
        handoff_to=row["handoff_to"],
        evidence_refs=tuple(json.loads(row["evidence_refs"])),
        requester_delivery_status=row["requester_delivery_status"],
        requester_delivery_turn_id=row["requester_delivery_turn_id"],
        requester_delivery_error=row["requester_delivery_error"],
        requester_delivered_at=row["requester_delivered_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def row_to_transition(row: Row) -> WorkItemTransition:
    """Decode one provider-transition row into its typed domain record."""
    raw_receipt = row["receipt"]
    receipt = json.loads(raw_receipt) if raw_receipt else None
    if receipt is not None and not isinstance(receipt, dict):
        raise TypeError("work_item_transitions.receipt must decode to an object")
    return WorkItemTransition(
        id=row["id"],
        execution_id=row["execution_id"],
        request_id=row["request_id"],
        operation=row["operation"],
        target_status=row["target_status"],
        result_execution_status=WorkItemExecutionStatus(row["result_execution_status"]),
        evidence_refs=tuple(json.loads(row["evidence_refs"])),
        summary=row["summary"],
        blocker=row["blocker"],
        handoff_to=row["handoff_to"],
        status=WorkItemTransitionStatus(row["status"]),
        receipt=receipt,
        error=row["error"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )
