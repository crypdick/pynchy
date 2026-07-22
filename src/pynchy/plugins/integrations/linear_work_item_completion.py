"""Trusted completion effects for Linear work items awaiting acceptance."""

from __future__ import annotations

from pynchy.plugins.integrations.github_pull_requests import (  # noqa: TC001, RUF100 - beartype resolves completion annotations at runtime.
    GitHubPullRequestRef,
)
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_work_item_provider import (
    linear_client,
    reconcile_work_item,
    transition_linked_work_item,
)
from pynchy.state import (
    WorkItemTransitionRequest,
    begin_work_item_transition,
    get_active_work_item_execution,
    get_latest_unresolved_work_item_transition,
    get_work_item_execution_for_evidence_ref,
    get_work_item_transition_by_request,
)
from pynchy.types import WorkItemExecution, WorkItemExecutionStatus


async def complete_merged_pull_request(
    workspace: str,
    pull_request: GitHubPullRequestRef,
    delivery_id: str,
) -> WorkItemExecution | None:
    """Complete the work item linked to an authenticated merged-PR delivery."""
    execution = await get_work_item_execution_for_evidence_ref(
        pull_request.url,
        workspace=workspace,
    )
    if execution is None:
        return None
    if execution.status is WorkItemExecutionStatus.COMPLETED:
        return execution
    if execution.status is WorkItemExecutionStatus.FAILED:
        raise LinearError("Merged-PR completion previously conflicted with Linear state")
    async with linear_client(workspace=workspace) as client:
        if execution.status is WorkItemExecutionStatus.UNKNOWN:
            transition = await get_latest_unresolved_work_item_transition(execution.id)
            if transition is None or transition.target_status != "done":
                raise LinearError("Merged-PR completion has an unrelated uncertain transition")
            reconciled = await reconcile_work_item(
                client,
                workspace,
                execution.linear_issue_id,
                transition,
            )
            if reconciled.status is not WorkItemExecutionStatus.COMPLETED:
                raise LinearError("Merged-PR completion could not be reconciled")
            return reconciled
        if execution.status is not WorkItemExecutionStatus.AWAITING_REVIEW:
            return None
        updated = await transition_linked_work_item(
            client,
            workspace,
            execution.linear_issue_id,
            WorkItemTransitionRequest(
                execution=execution,
                request_id=f"github-merge:{delivery_id}",
                operation="complete_after_pull_request_merge",
                target_status="done",
                result_execution_status=WorkItemExecutionStatus.COMPLETED,
                summary=execution.summary,
                evidence_refs=execution.evidence_refs,
            ),
            {"awaiting_review"},
        )
    if updated.status is WorkItemExecutionStatus.UNKNOWN:
        raise LinearError("Merged-PR completion outcome is unknown")
    if updated.status is WorkItemExecutionStatus.FAILED:
        raise LinearError("Linear state conflicted with merged-PR completion")
    return updated


async def complete_reviewed_work_item(
    workspace: str,
    issue_id: str,
    delivery_id: str,
) -> WorkItemExecution | None:
    """Complete a linked execution after Linear reports human acceptance."""
    execution = await get_active_work_item_execution(issue_id)
    if execution is None or execution.workspace != workspace:
        return None
    request_id = f"linear-review:{delivery_id}"
    transition = await get_work_item_transition_by_request(request_id)
    if execution.status is WorkItemExecutionStatus.UNKNOWN:
        transition = await get_latest_unresolved_work_item_transition(execution.id)
        if transition is None or transition.target_status != "done":
            return None
    elif execution.status is WorkItemExecutionStatus.AWAITING_REVIEW:
        if transition is None:
            transition = await begin_work_item_transition(
                WorkItemTransitionRequest(
                    execution=execution,
                    request_id=request_id,
                    operation="complete_after_linear_review",
                    target_status="done",
                    result_execution_status=WorkItemExecutionStatus.COMPLETED,
                    summary=execution.summary,
                    evidence_refs=execution.evidence_refs,
                )
            )
    else:
        return None
    async with linear_client(workspace=workspace) as client:
        resolved = await reconcile_work_item(client, workspace, issue_id, transition)
    if resolved.status is not WorkItemExecutionStatus.COMPLETED:
        raise LinearError("Linear review completion could not be reconciled")
    return resolved
