"""Cancel Linear-owned execution when its durable conversation is reset."""

from __future__ import annotations

import aiohttp
from temporalio.service import RPCError

from pynchy.host.orchestrator.temporal.scheduler import (
    TemporalRuntimeUnavailableError,
    cancel_scheduled_agent_workflow,
)
from pynchy.logger import logger
from pynchy.plugins.integrations.linear_client import LinearClient, LinearError
from pynchy.plugins.integrations.linear_work_item_provider import (
    linear_client,
    transition_linked_work_item,
)
from pynchy.state import (
    WorkItemTransitionRequest,
    cancel_task_and_checkpoint,
    cancel_work_item_execution,
    get_active_work_item_execution,
    get_conversation,
    get_conversation_control_by_thread,
)
from pynchy.types import (
    ChatJid,
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkspaceProfile,
)

_RESET_BLOCKER = (
    "The conversation context was reset during active execution. Pynchy cancelled "
    "this attempt and preserved its worktree changes. Move the issue from Blocked "
    "to Human Approved to start a new attempt and session."
)


async def _block_linear_issue(execution: WorkItemExecution) -> None:
    """Best-effort provider transition after the local attempt is stopped."""
    try:
        async with linear_client(workspace=execution.workspace) as client:
            await _apply_blocked_transition(client, execution)
    except (aiohttp.ClientError, LinearError, TimeoutError, ValueError) as exc:
        logger.warning(
            "Linear provider update failed during context reset",
            execution_id=execution.id,
            err=str(exc),
        )


async def _apply_blocked_transition(
    client: LinearClient,
    execution: WorkItemExecution,
) -> None:
    """Apply the provider transition and retain a human-readable explanation."""
    updated = await transition_linked_work_item(
        client,
        execution.workspace,
        execution.linear_issue_id,
        WorkItemTransitionRequest(
            execution=execution,
            request_id=f"context-reset:{execution.id}",
            operation="context_reset_cancel",
            target_status="blocked",
            result_execution_status=WorkItemExecutionStatus.CANCELLED,
            blocker=_RESET_BLOCKER,
        ),
        {"in_progress", "awaiting_review", "follow_ups", "blocked"},
    )
    if updated.status is WorkItemExecutionStatus.CANCELLED:
        await client.create_comment(execution.linear_issue_id, _RESET_BLOCKER)
        return
    logger.warning(
        "Linear reset transition did not reach Blocked",
        execution_id=execution.id,
        status=updated.status,
    )


async def cancel_linear_execution_for_reset(group: WorkspaceProfile) -> bool:
    """Cancel the active execution owned by a routed Linear issue conversation."""
    binding = await get_conversation_control_by_thread(ChatJid(group.jid))
    if binding is None:
        return False
    conversation = await get_conversation(binding.conversation_id)
    if conversation is None or not str(conversation.subject.namespace).startswith("linear:"):
        return False
    execution = await get_active_work_item_execution(str(conversation.subject.key))
    if execution is None:
        return False

    if execution.temporal_workflow_id is not None:
        try:
            await cancel_scheduled_agent_workflow(execution.temporal_workflow_id)
        except (RPCError, TemporalRuntimeUnavailableError) as exc:
            logger.warning(
                "Could not confirm Linear execution workflow cancellation",
                execution_id=execution.id,
                err=str(exc),
            )
    if execution.task_id is not None:
        await cancel_task_and_checkpoint(execution.task_id)

    await _block_linear_issue(execution)
    await cancel_work_item_execution(execution.id, blocker=_RESET_BLOCKER)
    return True
