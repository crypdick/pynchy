"""Durable provider-transition records for Linear work-item executions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite  # noqa: TC002 - beartype resolves this annotation at runtime.

from pynchy.conversation.api import (  # noqa: TC001 - beartype resolves annotations.
    ConversationLifecycleFence,
)
from pynchy.state.connection import _get_db, atomic_write
from pynchy.state.conversation_lifecycle_fences import lifecycle_fence_matches
from pynchy.state.work_item_models import (
    WorkItemTransitionRequest,
    WorkItemTransitionResolution,
)
from pynchy.state.work_item_rows import row_to_transition
from pynchy.state.work_item_transition_records import insert_work_item_transition
from pynchy.state.work_items import get_work_item_execution
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransition,
    WorkItemTransitionStatus,
)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


async def begin_work_item_transition(request: WorkItemTransitionRequest) -> WorkItemTransition:
    """Record a pending remote transition before Pynchy calls Linear."""
    now = _timestamp()
    async with atomic_write() as db:
        await _persist_work_item_transition(db, request, now)
    transition = await get_work_item_transition_by_request(request.request_id)
    if transition is None:
        raise RuntimeError("work item transition was not persisted")
    return transition


async def begin_work_item_transition_if_lifecycle_current(
    request: WorkItemTransitionRequest,
    *,
    lifecycle_fence: ConversationLifecycleFence,
) -> WorkItemTransition | None:
    """Persist a transition only while its terminal delivery still owns the conversation."""
    now = _timestamp()
    async with atomic_write() as db:
        if not await lifecycle_fence_matches(db, lifecycle_fence):
            return None
        await _persist_work_item_transition(db, request, now)
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
    execution = await _resolve_work_item_transition(
        WorkItemTransitionResolution(
            transition=transition,
            execution_status=execution_status,
            transition_status=transition_status,
            issue=issue,
            error=error,
        )
    )
    if execution is None:
        raise RuntimeError("Unfenced work item transition unexpectedly lost its lifecycle fence")
    return execution


async def resolve_work_item_transition_if_lifecycle_current(
    resolution: WorkItemTransitionResolution,
    *,
    lifecycle_fence: ConversationLifecycleFence,
) -> WorkItemExecution | None:
    """Settle a transition only while its terminal provider delivery is current."""
    return await _resolve_work_item_transition(
        resolution,
        lifecycle_fence=lifecycle_fence,
    )


async def get_work_item_transition_by_request(request_id: str) -> WorkItemTransition | None:
    """Return the persisted transition for a specific idempotency request."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM work_item_transitions WHERE request_id = ?", (request_id,)
    )
    row = await cursor.fetchone()
    return row_to_transition(row) if row else None


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


async def _persist_work_item_transition(
    database: aiosqlite.Connection,
    request: WorkItemTransitionRequest,
    created_at: str,
) -> None:
    """Store one pending transition and its requester-delivery metadata."""
    await insert_work_item_transition(
        database,
        request=request,
        created_at=created_at,
    )
    delivery_turn_id = request.requester_delivery_turn_id
    evidence_refs = (
        request.evidence_refs
        if request.evidence_refs is not None
        else request.execution.evidence_refs
    )
    await database.execute(
        """
        UPDATE work_item_executions
        SET summary = COALESCE(?, summary),
            blocker = COALESCE(?, blocker),
            handoff_to = COALESCE(?, handoff_to),
            evidence_refs = ?,
            requester_delivery_status = CASE WHEN ? IS NULL
                THEN requester_delivery_status ELSE 'pending' END,
            requester_delivery_turn_id = COALESCE(?, requester_delivery_turn_id),
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
            json.dumps(evidence_refs),
            delivery_turn_id,
            delivery_turn_id,
            delivery_turn_id,
            delivery_turn_id,
            created_at,
            request.execution.id,
        ),
    )


async def _resolve_work_item_transition(
    resolution: WorkItemTransitionResolution,
    *,
    lifecycle_fence: ConversationLifecycleFence | None = None,
) -> WorkItemExecution | None:
    """Persist a provider receipt only when an optional terminal fence still wins."""
    transition = resolution.transition
    execution_status = resolution.execution_status
    transition_status = resolution.transition_status
    issue = resolution.issue
    error = resolution.error
    now = _timestamp()
    if transition_status is WorkItemTransitionStatus.PENDING:
        raise ValueError("A work item transition cannot resolve to pending")
    clears_blocked_outcome = (
        transition_status is WorkItemTransitionStatus.SUCCEEDED
        and execution_status
        not in {
            WorkItemExecutionStatus.BLOCKED,
            WorkItemExecutionStatus.HANDED_OFF,
        }
    )
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
    async with atomic_write() as db:
        if lifecycle_fence is not None and not await lifecycle_fence_matches(db, lifecycle_fence):
            return None
        # Provider reconciliation may retry an unresolved intent, but its first
        # settled receipt owns the resulting execution projection.
        cursor = await db.execute(
            """
            UPDATE work_item_transitions
            SET status = ?, resolved_at = ?
            WHERE id = ?
              AND status IN (?, ?)
            """,
            (
                transition_status.value,
                now,
                transition.id,
                WorkItemTransitionStatus.PENDING.value,
                WorkItemTransitionStatus.UNKNOWN.value,
            ),
        )
        if cursor.rowcount == 1:
            receipt = json.dumps(issue, sort_keys=True) if issue is not None else None
            issue_projection: tuple[str, str, str, str, str | None] | None = None
            if issue is not None:
                state = _issue_state(issue)
                issue_projection = (
                    _issue_str(issue, "identifier"),
                    _issue_str(issue, "url"),
                    _issue_str(state, "id"),
                    _issue_str(state, "name"),
                    _optional_str(issue, "updatedAt"),
                )
            await db.execute(
                """
                UPDATE work_item_transitions
                SET receipt = ?, error = ?
                WHERE id = ?
                """,
                (receipt, error, transition.id),
            )
        if cursor.rowcount == 1 and issue is None:
            await db.execute(
                """
                UPDATE work_item_executions
                SET status = ?,
                    blocker = CASE WHEN ? THEN NULL ELSE blocker END,
                    handoff_to = CASE WHEN ? THEN NULL ELSE handoff_to END,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                  AND status NOT IN (?, ?, ?, ?)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM work_item_transitions AS newer
                      WHERE newer.execution_id = work_item_executions.id
                        AND newer.id > ?
                  )
                """,
                (
                    execution_status.value,
                    clears_blocked_outcome,
                    clears_blocked_outcome,
                    now,
                    completed_at,
                    transition.execution_id,
                    WorkItemExecutionStatus.COMPLETED.value,
                    WorkItemExecutionStatus.CANCELLED.value,
                    WorkItemExecutionStatus.HANDED_OFF.value,
                    WorkItemExecutionStatus.FAILED.value,
                    transition.id,
                ),
            )
        elif cursor.rowcount == 1 and issue_projection is not None:
            await db.execute(
                """
                UPDATE work_item_executions
                SET linear_issue_identifier = ?, linear_issue_url = ?,
                    observed_state_id = ?, observed_state_name = ?, observed_updated_at = ?,
                    status = ?,
                    blocker = CASE WHEN ? THEN NULL ELSE blocker END,
                    handoff_to = CASE WHEN ? THEN NULL ELSE handoff_to END,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                  AND status NOT IN (?, ?, ?, ?)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM work_item_transitions AS newer
                      WHERE newer.execution_id = work_item_executions.id
                        AND newer.id > ?
                  )
                """,
                (
                    *issue_projection,
                    execution_status.value,
                    clears_blocked_outcome,
                    clears_blocked_outcome,
                    now,
                    completed_at,
                    transition.execution_id,
                    WorkItemExecutionStatus.COMPLETED.value,
                    WorkItemExecutionStatus.CANCELLED.value,
                    WorkItemExecutionStatus.HANDED_OFF.value,
                    WorkItemExecutionStatus.FAILED.value,
                    transition.id,
                ),
            )
    execution = await get_work_item_execution(transition.execution_id)
    if execution is None:
        raise RuntimeError("work item execution disappeared during transition resolution")
    return execution


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
