"""Turn ownership for host-acquired work-item leases."""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.state.connection import atomic_write
from pynchy.state.work_items import get_work_item_execution
from pynchy.work_items.api import WorkItemExecution


async def bind_work_item_execution_to_turn(
    execution_id: str,
    *,
    turn_id: str,
    task_id: str | None,
) -> WorkItemExecution:
    """Attach a host-acquired lease to the agent turn that reports its result."""
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as db:
        cursor = await db.execute(
            """
            UPDATE work_item_executions
            SET turn_id = COALESCE(turn_id, ?),
                task_id = COALESCE(task_id, ?),
                updated_at = ?
            WHERE id = ?
              AND (turn_id IS NULL OR turn_id = ?)
            """,
            (turn_id, task_id, now, execution_id, turn_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Linear work item lease belongs to another agent turn")
    execution = await get_work_item_execution(execution_id)
    if execution is None:
        raise RuntimeError("work item execution disappeared while binding its turn")
    return execution


async def bind_work_item_execution_to_task(
    execution_id: str,
    *,
    task_id: str,
    temporal_workflow_id: str,
) -> WorkItemExecution:
    """Attach a host-acquired lease to its durable Temporal-owned task.

    Webhook admission deliberately acquires the authorization lease before the
    periodic controller creates agent work. This binding closes that handoff
    without allowing a different task to take over an existing execution.
    The workflow ID may change when the controller reactivates the same task
    after an apparently successful run left the issue in progress.
    """
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as db:
        cursor = await db.execute(
            """
            UPDATE work_item_executions
            SET task_id = COALESCE(task_id, ?),
                temporal_workflow_id = ?,
                updated_at = ?
            WHERE id = ?
              AND (task_id IS NULL OR task_id = ?)
            """,
            (task_id, temporal_workflow_id, now, execution_id, task_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Linear work item lease belongs to another durable task")
    execution = await get_work_item_execution(execution_id)
    if execution is None:
        raise RuntimeError("work item execution disappeared while binding its task")
    return execution
