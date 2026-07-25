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
from pynchy.state.work_item_rows import row_to_execution, row_to_transition
from pynchy.types import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransition,
    WorkItemTransitionStatus,
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
                requester_delivery_error, requester_delivered_at, created_at, updated_at,
                completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                execution.requester_delivery_error,
                execution.requester_delivered_at,
                execution.created_at,
                execution.updated_at,
                execution.completed_at,
            ),
        )
        await _insert_transition(
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


async def begin_work_item_transition(request: WorkItemTransitionRequest) -> WorkItemTransition:
    """Record a pending remote transition before Pynchy calls Linear."""
    now = _timestamp()
    async with atomic_write() as db:
        await _insert_transition(
            db,
            request=request,
            created_at=now,
        )
        delivery_status = _delivery_status_for_operation(request.operation)
        await db.execute(
            """
            UPDATE work_item_executions
            SET summary = COALESCE(?, summary),
                blocker = ?,
                handoff_to = ?,
                evidence_refs = ?,
                requester_delivery_status = COALESCE(?, requester_delivery_status),
                requester_delivery_error = CASE WHEN ? IS NULL
                    THEN requester_delivery_error ELSE NULL END,
                requester_delivered_at = CASE WHEN ? IS NULL
                    THEN requester_delivered_at ELSE NULL END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                request.summary,
                request.blocker,
                request.handoff_to,
                json.dumps(request.evidence_refs),
                delivery_status,
                delivery_status,
                delivery_status,
                now,
                request.execution.id,
            ),
        )
    transition = await get_work_item_transition_by_request(request.request_id)
    if transition is None:
        raise RuntimeError("work item transition was not persisted")
    return transition


async def resolve_work_item_transition(
    *,
    transition: WorkItemTransition,
    execution_status: WorkItemExecutionStatus,
    transition_status: WorkItemTransitionStatus,
    issue: dict[str, Any] | None = None,
    error: str | None = None,
) -> WorkItemExecution:
    """Persist a provider receipt or uncertainty and the resulting local lifecycle state."""
    now = _timestamp()
    completed_at = (
        now
        if execution_status
        in {
            WorkItemExecutionStatus.COMPLETED,
            WorkItemExecutionStatus.CANCELLED,
            WorkItemExecutionStatus.BLOCKED,
            WorkItemExecutionStatus.HANDED_OFF,
            WorkItemExecutionStatus.FAILED,
        }
        else None
    )
    receipt = json.dumps(issue, sort_keys=True) if issue is not None else None
    async with atomic_write() as db:
        await db.execute(
            """
            UPDATE work_item_transitions
            SET status = ?, receipt = ?, error = ?, resolved_at = ?
            WHERE id = ?
            """,
            (transition_status.value, receipt, error, now, transition.id),
        )
        if issue is None:
            await db.execute(
                """
                UPDATE work_item_executions
                SET status = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (execution_status.value, now, completed_at, transition.execution_id),
            )
        else:
            state = _issue_state(issue)
            await db.execute(
                """
                UPDATE work_item_executions
                SET linear_issue_identifier = ?, linear_issue_url = ?,
                    observed_state_id = ?, observed_state_name = ?, observed_updated_at = ?,
                    status = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    _issue_str(issue, "identifier"),
                    _issue_str(issue, "url"),
                    _issue_str(state, "id"),
                    _issue_str(state, "name"),
                    _optional_str(issue, "updatedAt"),
                    execution_status.value,
                    now,
                    completed_at,
                    transition.execution_id,
                ),
            )
    execution = await get_work_item_execution(transition.execution_id)
    if execution is None:
        raise RuntimeError("work item execution disappeared during transition resolution")
    return execution


async def get_work_item_transition_by_request(request_id: str) -> WorkItemTransition | None:
    """Return the persisted transition for a specific idempotency request."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM work_item_transitions WHERE request_id = ?", (request_id,)
    )
    row = await cursor.fetchone()
    return row_to_transition(row) if row else None


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
        WHERE turn_id = ? AND requester_delivery_status = 'pending'
        """,
        (now, now, turn_id),
    )
    await db.commit()


async def get_latest_unresolved_work_item_transition(
    execution_id: str,
) -> WorkItemTransition | None:
    """Return the latest transition whose provider outcome needs reconciliation."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM work_item_transitions
        WHERE execution_id = ? AND status IN ('pending', 'unknown')
        ORDER BY id DESC
        LIMIT 1
        """,
        (execution_id,),
    )
    row = await cursor.fetchone()
    return row_to_transition(row) if row else None


async def _insert_transition(
    db: aiosqlite.Connection,
    *,
    request: WorkItemTransitionRequest,
    created_at: str,
) -> None:
    await db.execute(
        """
        INSERT INTO work_item_transitions (
            execution_id, request_id, operation, target_status,
            result_execution_status, evidence_refs, status, receipt, error, created_at, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
        """,
        (
            request.execution.id,
            request.request_id,
            request.operation,
            request.target_status,
            request.result_execution_status.value,
            json.dumps(request.evidence_refs),
            WorkItemTransitionStatus.PENDING.value,
            created_at,
        ),
    )


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


def _delivery_status_for_operation(operation: str) -> str | None:
    """Reserve a requester-delivery outcome for operations with a user summary."""
    if operation in {"complete_after_linear_review", "complete_after_pull_request_merge"}:
        return None
    return "not_requested" if operation == "claim" else "pending"
