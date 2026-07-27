"""Completion effects when Linear reports a work item in Done."""

from __future__ import annotations

from pynchy.conversation.models import (  # noqa: TC001, RUF100 - beartype resolves annotations.
    ConversationLifecycleFence,
)
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_work_item_provider import (
    linear_client,
    reconcile_work_item,
)
from pynchy.state import (
    WorkItemTransitionRequest,
    begin_work_item_transition,
    begin_work_item_transition_if_lifecycle_current,
    get_latest_unresolved_work_item_transition,
    get_work_item_execution_for_issue,
    get_work_item_transition_by_request,
)
from pynchy.types import WorkItemExecution, WorkItemExecutionStatus


async def complete_reviewed_work_item(
    execution_workspace: str,
    issue_id: str,
    delivery_id: str,
    *,
    lifecycle_fence: ConversationLifecycleFence | None = None,
    controller_workspace: str | None = None,
) -> WorkItemExecution | None:
    """Complete owner-local work against the board that reported Done."""
    execution = await get_work_item_execution_for_issue(issue_id, workspace=execution_workspace)
    if execution is None:
        return None
    request_id = f"linear-review:{delivery_id}"
    transition = await get_work_item_transition_by_request(request_id)
    if execution.status is WorkItemExecutionStatus.UNKNOWN:
        transition = await get_latest_unresolved_work_item_transition(execution.id)
        if transition is None or transition.target_status != "done":
            return None
    elif execution.status in {
        WorkItemExecutionStatus.IN_PROGRESS,
        WorkItemExecutionStatus.AWAITING_REVIEW,
        WorkItemExecutionStatus.FOLLOW_UPS,
        WorkItemExecutionStatus.BLOCKED,
    }:
        if transition is None:
            request = WorkItemTransitionRequest(
                execution=execution,
                request_id=request_id,
                operation="complete_after_linear_done",
                target_status="done",
                result_execution_status=WorkItemExecutionStatus.COMPLETED,
                summary=execution.summary,
                evidence_refs=execution.evidence_refs,
            )
            if lifecycle_fence is None:
                transition = await begin_work_item_transition(request)
            else:
                transition = await begin_work_item_transition_if_lifecycle_current(
                    request,
                    lifecycle_fence=lifecycle_fence,
                )
                if transition is None:
                    return None
    else:
        return None
    board_workspace = controller_workspace or execution_workspace
    async with linear_client(workspace=board_workspace) as client:
        resolved = await reconcile_work_item(
            client,
            board_workspace,
            issue_id,
            transition,
            lifecycle_fence=lifecycle_fence,
        )
    if resolved is None:
        return None
    if resolved.status is not WorkItemExecutionStatus.COMPLETED:
        raise LinearError("Linear review completion could not be reconciled")
    return resolved
