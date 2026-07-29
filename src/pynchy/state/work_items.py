"""Durable execution and provider-transition records for Linear work items."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from pynchy.state.connection import _get_db, atomic_write
from pynchy.state.work_item_models import (
    WorkItemClaimConflictError,
    WorkItemClaimRequest,
    WorkItemTransitionRequest,
)
from pynchy.state.work_item_rows import row_to_execution
from pynchy.state.work_item_transition_records import insert_work_item_transition
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


async def get_work_item_execution(execution_id: str) -> WorkItemExecution | None:
    """Return one execution record by its durable identifier."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM work_item_executions WHERE id = ?", (execution_id,))
    row = await cursor.fetchone()
    return row_to_execution(row) if row else None


async def get_active_work_item_execution(issue_id: str) -> WorkItemExecution | None:
    """Return the single execution that currently owns an issue, if any."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM work_item_executions
        WHERE linear_issue_id = ?
          AND status IN ('claiming', 'in_progress', 'unknown')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (issue_id,),
    )
    row = await cursor.fetchone()
    return row_to_execution(row) if row else None


async def get_unfinished_work_item_execution(issue_id: str) -> WorkItemExecution | None:
    """Return the newest execution when a terminal provider state must still retire it."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM (
            SELECT * FROM work_item_executions
            WHERE linear_issue_id = ?
            ORDER BY created_at DESC
            LIMIT 1
        )
        WHERE status IN (
            'claiming', 'in_progress', 'awaiting_review', 'follow_ups', 'blocked', 'unknown'
        )
        """,
        (issue_id,),
    )
    row = await cursor.fetchone()
    return row_to_execution(row) if row else None


async def get_work_item_execution_for_issue(
    issue_id: str,
    *,
    workspace: str,
) -> WorkItemExecution | None:
    """Return the most recent workspace-owned execution for an issue."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM work_item_executions
        WHERE linear_issue_id = ? AND workspace = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (issue_id, workspace),
    )
    row = await cursor.fetchone()
    return row_to_execution(row) if row else None


async def get_work_item_execution_for_task(task_id: str) -> WorkItemExecution | None:
    """Return the most recent Linear execution bound to a durable task."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM work_item_executions
        WHERE task_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (task_id,),
    )
    row = await cursor.fetchone()
    return row_to_execution(row) if row else None


async def list_work_item_executions(
    *,
    workspace: str | None = None,
    limit: int = 100,
) -> list[WorkItemExecution]:
    """Return bounded operator projections, newest execution first."""
    db = _get_db()
    if workspace:
        cursor = await db.execute(
            """
            SELECT * FROM work_item_executions
            WHERE workspace = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (workspace, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM work_item_executions ORDER BY updated_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    return [row_to_execution(row) for row in await cursor.fetchall()]


async def create_work_item_claim(request: WorkItemClaimRequest) -> WorkItemExecution:
    """Persist a local claim and its intended In Progress transition atomically."""
    execution_id = uuid.uuid4().hex
    now = _timestamp()
    state = _issue_state(request.issue)
    execution = WorkItemExecution(
        id=execution_id,
        workspace=request.workspace,
        linear_issue_id=_issue_str(request.issue, "id"),
        linear_issue_identifier=_issue_str(request.issue, "identifier"),
        linear_issue_url=_issue_str(request.issue, "url"),
        turn_id=request.turn_id,
        task_id=request.task_id,
        attempt=0,
        flow_id=None,
        temporal_workflow_id=None,
        initiated_by=request.initiated_by,
        observed_state_id=_issue_str(state, "id"),
        observed_state_name=_issue_str(state, "name"),
        observed_updated_at=_optional_str(request.issue, "updatedAt"),
        status=WorkItemExecutionStatus.CLAIMING,
        summary=None,
        blocker=None,
        handoff_to=None,
        evidence_refs=(),
        requester_delivery_status="not_requested",
        requester_delivery_turn_id=None,
        requester_delivery_error=None,
        requester_delivered_at=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    try:
        execution = await _persist_work_item_claim(execution, request, now)
    except aiosqlite.IntegrityError as exc:
        existing = await get_active_work_item_execution(execution.linear_issue_id)
        if existing is not None:
            raise WorkItemClaimConflictError(existing) from exc
        raise
    return execution


async def _persist_work_item_claim(
    execution: WorkItemExecution,
    request: WorkItemClaimRequest,
    now: str,
) -> WorkItemExecution:
    async with atomic_write() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM work_item_executions WHERE linear_issue_id = ?",
            (execution.linear_issue_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("work item attempt count query returned no row")
        execution = replace(execution, attempt=int(row[0]) + 1)
        await db.execute(
            """
            INSERT INTO work_item_executions (
                id, workspace, linear_issue_id, linear_issue_identifier, linear_issue_url,
                turn_id, task_id, attempt, flow_id, temporal_workflow_id, initiated_by,
                observed_state_id, observed_state_name, observed_updated_at, status,
                summary, blocker, handoff_to, evidence_refs, requester_delivery_status,
                requester_delivery_turn_id, requester_delivery_error, requester_delivered_at,
                created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution.id,
                execution.workspace,
                execution.linear_issue_id,
                execution.linear_issue_identifier,
                execution.linear_issue_url,
                execution.turn_id,
                execution.task_id,
                execution.attempt,
                execution.flow_id,
                execution.temporal_workflow_id,
                execution.initiated_by,
                execution.observed_state_id,
                execution.observed_state_name,
                execution.observed_updated_at,
                execution.status.value,
                execution.summary,
                execution.blocker,
                execution.handoff_to,
                json.dumps(execution.evidence_refs),
                execution.requester_delivery_status,
                execution.requester_delivery_turn_id,
                execution.requester_delivery_error,
                execution.requester_delivered_at,
                execution.created_at,
                execution.updated_at,
                execution.completed_at,
            ),
        )
        await insert_work_item_transition(
            db,
            request=WorkItemTransitionRequest(
                execution=execution,
                request_id=request.request_id,
                operation="claim",
                target_status="in_progress",
                result_execution_status=WorkItemExecutionStatus.IN_PROGRESS,
            ),
            created_at=now,
        )
    return execution


async def mark_work_item_delivery_delivered_for_turn(turn_id: str) -> None:
    """Mark the linked requester delivery separately after a visible final result."""
    db = _get_db()
    now = _timestamp()
    await db.execute(
        """
        UPDATE work_item_executions
        SET requester_delivery_status = 'delivered',
            requester_delivery_error = NULL,
            requester_delivered_at = ?,
            updated_at = ?
        WHERE requester_delivery_turn_id = ? AND requester_delivery_status = 'pending'
        """,
        (now, now, turn_id),
    )
    await db.commit()


def _issue_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Linear issue payload missing {key}")
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _issue_state(issue: dict[str, Any]) -> dict[str, Any]:
    state = issue.get("state")
    if not isinstance(state, dict):
        raise TypeError("Linear issue payload missing state")
    return state
