"""Schema migrations owned by durable work-item execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def migrate_work_item_active_index(database: aiosqlite.Connection) -> None:
    """Keep execution leases limited to work that can still be running."""
    await database.execute("DROP INDEX IF EXISTS idx_work_item_executions_active_issue")
    await database.execute("DROP INDEX IF EXISTS idx_work_item_executions_active_issue_v2")
    await database.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_work_item_executions_active_issue_v3
        ON work_item_executions(linear_issue_id)
        WHERE status IN ('claiming', 'in_progress', 'unknown')
        """
    )
    await database.commit()
