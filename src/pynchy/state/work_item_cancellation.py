"""Operator-owned cancellation of durable work-item execution."""

from __future__ import annotations

from datetime import UTC, datetime

from aiosqlite import Connection  # noqa: TC002, RUF100 - beartype resolves annotations.

from pynchy.conversation.api import (  # noqa: TC001, RUF100 - beartype resolves annotations.
    ConversationLifecycleFence,
)
from pynchy.state.connection import atomic_write
from pynchy.state.conversation_lifecycle_fences import lifecycle_fence_matches
from pynchy.state.work_item_rows import row_to_execution
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
)


async def cancel_work_item_execution(
    execution_id: str,
    *,
    blocker: str,
) -> WorkItemExecution:
    """Force a local execution terminal after an operator-owned context reset."""
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as database:
        return await _cancel_work_item_execution(database, execution_id, blocker, now)


async def cancel_work_item_execution_if_lifecycle_current(
    execution_id: str,
    *,
    blocker: str,
    lifecycle_fence: ConversationLifecycleFence,
) -> WorkItemExecution | None:
    """Cancel only while this exact terminal lifecycle delivery remains current."""
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as database:
        if not await lifecycle_fence_matches(database, lifecycle_fence):
            return None
        return await _cancel_work_item_execution(database, execution_id, blocker, now)


async def _cancel_work_item_execution(
    database: Connection,
    execution_id: str,
    blocker: str,
    now: str,
) -> WorkItemExecution:
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
