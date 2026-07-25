"""Turn ownership for host-acquired work-item leases."""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.state.connection import atomic_write
from pynchy.state.work_items import get_work_item_execution
from pynchy.types import WorkItemExecution  # noqa: TC001 - beartype resolves annotations.


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
