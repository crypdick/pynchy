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


async def migrate_work_item_outcome_projection(database: aiosqlite.Connection) -> None:
    """Separate historical blocked evidence from the current execution projection.

    Older rows kept ``blocker`` and ``handoff_to`` on the mutable execution after
    successful non-blocked transitions. Preserve that evidence on the latest
    successful blocked transition before clearing statuses whose current
    projection cannot be blocked.
    """
    await database.execute(
        """
        UPDATE work_item_transitions
        SET blocker = COALESCE(
                blocker,
                (
                    SELECT execution.blocker
                    FROM work_item_executions AS execution
                    WHERE execution.id = work_item_transitions.execution_id
                )
            ),
            handoff_to = COALESCE(
                handoff_to,
                (
                    SELECT execution.handoff_to
                    FROM work_item_executions AS execution
                    WHERE execution.id = work_item_transitions.execution_id
                )
            )
        WHERE status = 'succeeded'
          AND result_execution_status IN ('blocked', 'handed_off')
          AND id = (
              SELECT MAX(candidate.id)
              FROM work_item_transitions AS candidate
              WHERE candidate.execution_id = work_item_transitions.execution_id
                AND candidate.status = 'succeeded'
                AND candidate.result_execution_status IN ('blocked', 'handed_off')
          )
          AND EXISTS (
              SELECT 1
              FROM work_item_executions AS execution
              WHERE execution.id = work_item_transitions.execution_id
                AND (execution.blocker IS NOT NULL OR execution.handoff_to IS NOT NULL)
          )
        """
    )
    await database.execute(
        """
        UPDATE work_item_executions
        SET blocker = NULL, handoff_to = NULL
        WHERE status IN (
            'claiming',
            'in_progress',
            'awaiting_review',
            'follow_ups',
            'completed',
            'cancelled'
        )
          AND (blocker IS NOT NULL OR handoff_to IS NOT NULL)
        """
    )
    await database.commit()
