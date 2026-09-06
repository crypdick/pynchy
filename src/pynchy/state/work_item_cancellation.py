"""Operator-owned cancellation of durable work-item execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from pynchy.conversation.api import (
    ConversationLifecycleFence,
)
from pynchy.state.connection import atomic_write
from pynchy.state.conversation_lifecycle_fences import lifecycle_fence_matches
from pynchy.state.work_item_rows import row_to_execution
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
)

if TYPE_CHECKING:
    from aiosqlite import Row


@runtime_checkable
class _CancellationCursor(Protocol):
    rowcount: int

    async def fetchone(self) -> object: ...


@runtime_checkable
class _CancellationDatabase(Protocol):
    async def execute(self, *args: object) -> _CancellationCursor: ...


async def cancel_work_item_execution(
    execution_id: str,
    *,
    blocker: str,
) -> WorkItemExecution:
    """Force a local execution terminal after an operator-owned context reset."""
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as database:
        return await _cancel_work_item_execution(
            cast("_CancellationDatabase", database), execution_id, blocker, now
        )


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
        return await _cancel_work_item_execution(
            cast("_CancellationDatabase", database), execution_id, blocker, now
        )


async def _cancel_work_item_execution(
    database: _CancellationDatabase,
    execution_id: str,
    blocker: str,
    now: str,
) -> WorkItemExecution:
    update_cursor = await database.execute(
        """
        UPDATE work_item_executions
        SET status = ?, blocker = ?, updated_at = ?, completed_at = ?
        WHERE id = ?
          AND status IN (?, ?, ?, ?, ?, ?)
        """,
        (
            WorkItemExecutionStatus.CANCELLED.value,
            blocker,
            now,
            now,
            execution_id,
            WorkItemExecutionStatus.CLAIMING.value,
            WorkItemExecutionStatus.IN_PROGRESS.value,
            WorkItemExecutionStatus.AWAITING_REVIEW.value,
            WorkItemExecutionStatus.FOLLOW_UPS.value,
            WorkItemExecutionStatus.BLOCKED.value,
            WorkItemExecutionStatus.UNKNOWN.value,
        ),
    )
    cursor = await database.execute(
        "SELECT * FROM work_item_executions WHERE id = ?",
        (execution_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        if update_cursor.rowcount == 1:
            raise RuntimeError("Linear work item execution disappeared during cancellation")
        raise ValueError("Linear work item execution does not exist")
    return row_to_execution(cast("Row", row))
