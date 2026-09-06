"""Immutable intent records for Linear work-item provider transitions."""

from __future__ import annotations

import json

import aiosqlite

from pynchy.work_items.api import (
    WorkItemTransitionRequest,
    WorkItemTransitionStatus,
)


async def insert_work_item_transition(
    database: aiosqlite.Connection,
    *,
    request: WorkItemTransitionRequest,
    created_at: str,
) -> None:
    """Persist one pending provider transition with its immutable outcome evidence."""
    evidence_refs = (
        request.evidence_refs
        if request.evidence_refs is not None
        else request.execution.evidence_refs
    )
    await database.execute(
        """
        INSERT INTO work_item_transitions (
            execution_id, request_id, operation, target_status,
            result_execution_status, evidence_refs, summary, blocker, handoff_to,
            status, receipt, error, created_at, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
        """,
        (
            request.execution.id,
            request.request_id,
            request.operation,
            request.target_status,
            request.result_execution_status.value,
            json.dumps(evidence_refs),
            request.summary,
            request.blocker,
            request.handoff_to,
            WorkItemTransitionStatus.PENDING.value,
            created_at,
        ),
    )
