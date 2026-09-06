"""Atomic execution fence for provider-authoritative terminal recovery."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)

from pynchy.conversation.api import (
    ConversationId,
    TerminalConversationRetirement,
)
from pynchy.state.connection import atomic_write
from pynchy.state.conversation_controls import _retire_conversation_for_terminal
from pynchy.work_items.api import (
    WorkItemExecution,
)


async def retire_latest_terminal_work_item_conversation(
    conversation_id: ConversationId,
    execution: WorkItemExecution,
    *,
    control_state_revision: str | None,
) -> TerminalConversationRetirement | None:
    """Apply provider closure only while this exact terminal execution is latest."""
    async with atomic_write() as database:
        cursor = await database.execute(
            """
            WITH ranked AS (
                SELECT id, status,
                       ROW_NUMBER() OVER (
                           PARTITION BY linear_issue_id
                           ORDER BY attempt DESC, created_at DESC, id DESC
                       ) AS execution_rank
                FROM work_item_executions
            )
            SELECT 1
            FROM work_item_executions AS current
            JOIN ranked ON ranked.id = current.id
            JOIN routed_conversations AS conversation
              ON conversation.id = ?
             AND conversation.workspace = current.workspace
             AND conversation.subject_key = current.linear_issue_id
             AND conversation.subject_namespace LIKE 'linear:%:issue'
            WHERE current.id = ?
              AND ranked.execution_rank = 1
              AND ranked.status IN ('completed', 'cancelled', 'handed_off', 'failed')
            """,
            (conversation_id, execution.id),
        )
        if await cursor.fetchone() is None:
            return None
        return await _retire_conversation_for_terminal(
            database,
            conversation_id,
            preserve_delivery=None,
            control_state_revision=control_state_revision,
        )


async def retire_terminal_execution_resources_if_unowned(
    execution: WorkItemExecution,
    retire_runtime: Callable[[], Awaitable[None]],
) -> bool:
    """Fence ownership through runtime cancellation and durable retirement."""
    async with atomic_write() as database:
        cursor = await database.execute(
            """
            WITH ranked AS (
                SELECT id, status,
                       ROW_NUMBER() OVER (
                           PARTITION BY linear_issue_id
                           ORDER BY attempt DESC, created_at DESC, id DESC
                       ) AS execution_rank
                FROM work_item_executions
            )
            SELECT 1
            FROM work_item_executions AS current
            JOIN ranked ON ranked.id = current.id
            WHERE current.id = ?
              AND ranked.status IN ('completed', 'cancelled', 'handed_off', 'failed')
              AND NOT EXISTS (
                  SELECT 1
                  FROM work_item_executions AS other
                  WHERE other.id != current.id
                    AND (
                        (current.task_id IS NOT NULL AND other.task_id = current.task_id)
                        OR (current.turn_id IS NOT NULL AND other.turn_id = current.turn_id)
                    )
              )
            """,
            (execution.id,),
        )
        if await cursor.fetchone() is None:
            return False
        # Keep the write lock through external cancellation so a reused task or
        # turn cannot appear between the ownership check and durable retirement.
        # Failure rolls back, leaving the exact residue as a retry marker.
        await retire_runtime()
        if execution.task_id is not None:
            await database.execute(
                """
                UPDATE scheduled_tasks
                SET status = 'cancelled', next_run = NULL
                WHERE id = ? AND status IN ('active', 'paused')
                """,
                (execution.task_id,),
            )
            await database.execute(
                "DELETE FROM in_flight_turns WHERE task_id = ?",
                (execution.task_id,),
            )
        if execution.turn_id is not None:
            await database.execute(
                "DELETE FROM in_flight_turns WHERE turn_id = ?",
                (execution.turn_id,),
            )
        return True
