"""Operator-owned cancellation of durable work-item execution."""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.state.connection import atomic_write
from pynchy.state.work_item_rows import row_to_execution
from pynchy.types import WorkItemExecution, WorkItemExecutionStatus


async def cancel_work_item_execution(
    execution_id: str,
    *,
    blocker: str,
) -> WorkItemExecution:
    """Force a local execution terminal after an operator-owned context reset."""
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as database:
        cursor = await database.execute(
            """
            UPDATE work_item_executions
            SET status = ?, blocker = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (WorkItemExecutionStatus.CANCELLED.value, blocker, now, now, execution_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Linear work item execution does not exist")
        cursor = await database.execute(
            "SELECT * FROM work_item_executions WHERE id = ?",
            (execution_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("Linear work item execution disappeared during cancellation")
    return row_to_execution(row)
