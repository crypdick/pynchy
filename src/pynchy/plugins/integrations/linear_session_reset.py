"""Cancel Linear-owned execution when its durable conversation is reset."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves cancellation annotations at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass

import aiohttp

from pynchy.conversation.api import (  # noqa: TC001 - beartype resolves contract annotations at runtime.
    Conversation,
    ConversationControlBinding,
    ConversationId,
)
from pynchy.identifiers import ChatJid
from pynchy.logger import logger
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_work_item_provider import (
    linear_client,
    transition_linked_work_item,
)
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransitionRequest,
)
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)

_RESET_BLOCKER = (
    "The conversation context was reset during active execution. Pynchy cancelled "
    "this attempt and preserved its worktree changes. Move the issue from Blocked "
    "to Human Approved to start a new attempt and session."
)


@dataclass(frozen=True)
class LinearSessionResetState:
    """Durable operations needed to settle a Linear-owned execution."""

    get_control_by_thread: Callable[[ChatJid], Awaitable[ConversationControlBinding | None]]
    get_conversation: Callable[[ConversationId], Awaitable[Conversation | None]]
    get_active_execution: Callable[[str], Awaitable[WorkItemExecution | None]]
    cancel_task: Callable[[str], Awaitable[None]]
    cancel_execution: Callable[..., Awaitable[WorkItemExecution]]


async def _block_linear_issue(execution: WorkItemExecution) -> None:
    """Best-effort provider transition after the local attempt is stopped."""
    try:  # noqa: PLW0717 - provider transition and explanatory comment share one failure boundary.
        async with linear_client(workspace=execution.workspace) as client:
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
            else:
                logger.warning(
                    "Linear reset transition did not reach Blocked",
                    execution_id=execution.id,
                    status=updated.status,
                )
    except (aiohttp.ClientError, LinearError, TimeoutError, ValueError) as exc:
        logger.warning(
            "Linear provider update failed during context reset",
            execution_id=execution.id,
            err=str(exc),
        )


async def cancel_linear_execution_for_reset(
    group: WorkspaceProfile,
    *,
    cancel_scheduled_workflow: Callable[[str], Awaitable[bool]],
    state: LinearSessionResetState,
) -> bool:
    """Cancel the active execution owned by a routed Linear issue conversation."""
    binding = await state.get_control_by_thread(ChatJid(group.jid))
    if binding is None:
        return False
    conversation = await state.get_conversation(binding.conversation_id)
    if conversation is None or not str(conversation.subject.namespace).startswith("linear:"):
        return False
    execution = await state.get_active_execution(str(conversation.subject.key))
    if execution is None:
        return False

    if execution.temporal_workflow_id is not None and not await cancel_scheduled_workflow(
        execution.temporal_workflow_id
    ):
        logger.warning(
            "Could not confirm Linear execution workflow cancellation",
            execution_id=execution.id,
        )
    if execution.task_id is not None:
        await state.cancel_task(execution.task_id)

    await _block_linear_issue(execution)
    await state.cancel_execution(execution.id, blocker=_RESET_BLOCKER)
    return True
