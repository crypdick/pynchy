"""Completion effects when Linear reports a work item in Done."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves configured completion callbacks at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass

from pynchy.conversation.api import (  # noqa: TC001 - beartype resolves annotations.
    ConversationLifecycleFence,
)
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_work_item_provider import (
    linear_client,
    reconcile_work_item,
)
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransition,
    WorkItemTransitionRequest,
)


@dataclass(frozen=True)
class LinearWorkItemCompletionRuntime:
    """Durable transition operations selected during Linear plugin composition."""

    get_execution_for_issue: Callable[..., Awaitable[WorkItemExecution | None]]
    get_transition_by_request: Callable[[str], Awaitable[WorkItemTransition | None]]
    get_latest_unresolved_transition: Callable[[str], Awaitable[WorkItemTransition | None]]
    begin_transition: Callable[[WorkItemTransitionRequest], Awaitable[WorkItemTransition]]
    begin_transition_if_lifecycle_current: Callable[..., Awaitable[WorkItemTransition | None]]


_runtime: LinearWorkItemCompletionRuntime | None = None


def configure_linear_work_item_completion_runtime(
    runtime: LinearWorkItemCompletionRuntime,
) -> None:
    """Set durable work-item transition operations for Linear Done callbacks."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> LinearWorkItemCompletionRuntime:
    if _runtime is None:
        raise RuntimeError("Linear work-item completion runtime has not been configured")
    return _runtime


async def complete_reviewed_work_item(
    execution_workspace: str,
    issue_id: str,
    delivery_id: str,
    *,
    lifecycle_fence: ConversationLifecycleFence | None = None,
    controller_workspace: str | None = None,
) -> WorkItemExecution | None:
    """Complete owner-local work against the board that reported Done."""
    runtime = _configured_runtime()
    execution = await runtime.get_execution_for_issue(
        issue_id,
        workspace=execution_workspace,
    )
    if execution is None:
        return None
    request_id = f"linear-review:{delivery_id}"
    transition = await runtime.get_transition_by_request(request_id)
    if execution.status is WorkItemExecutionStatus.UNKNOWN:
        transition = await runtime.get_latest_unresolved_transition(execution.id)
        if transition is None or transition.target_status != "done":
            return None
    elif execution.status in {
        WorkItemExecutionStatus.CLAIMING,
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
                transition = await runtime.begin_transition(request)
            else:
                transition = await runtime.begin_transition_if_lifecycle_current(
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
