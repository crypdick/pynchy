"""Host-owned lifecycle operations for Pynchy executions linked to Linear issues.

The ordinary Linear MCP server remains useful for browsing and planning.  These
operations deliberately run in the host process because a claim and its
provider-transition evidence must share Pynchy's durable state and policy
boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pynchy.plugins.integrations.github_pull_requests import GitHubPullRequestRef
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_statuses import AGENT_SETTABLE_STATUSES
from pynchy.plugins.integrations.linear_work_item_provider import (
    claim_work_item,
    linear_client,
    move_unlinked_work_item,
    reconcile_work_item,
    transition_linked_work_item,
)
from pynchy.state import (
    WorkItemClaimConflictError,
    WorkItemTransitionRequest,
    get_active_work_item_execution,
    get_in_flight_turn_for_group,
    get_latest_unresolved_work_item_transition,
    get_work_item_execution_for_evidence_ref,
    get_work_item_execution_for_issue,
    list_work_item_executions,
)
from pynchy.types import (
    WorkItemExecution,
    WorkItemExecutionStatus,
)

_WORKSPACE_REQUIRED = "source_group is required"
_ISSUE_REQUIRED = "issue_id is required"
_SUMMARY_REQUIRED = "summary is required"
_BLOCKER_REQUIRED = "reason is required"
_HANDOFF_REQUIRED = "owner is required"
_ACTIVE_EXECUTION_REQUIRED = "No active Pynchy execution owns this Linear work item"
_UNKNOWN_TRANSITION_REQUIRED = "No uncertain work-item transition needs reconciliation"
_AGENT_SETTABLE_STATUS_ERROR = "Agents may move unlinked Linear items only to: {statuses}"


@dataclass(frozen=True)
class _LinkedTransitionSpec:
    """Fixed lifecycle semantics for one user-facing work-item operation."""

    operation: str
    target_status: str
    result_status: WorkItemExecutionStatus
    expected_statuses: set[str]


@dataclass(frozen=True)
class _TransitionDetails:
    """User-supplied result metadata for a typed lifecycle operation."""

    summary: str | None = None
    blocker: str | None = None
    handoff_to: str | None = None
    evidence_refs: tuple[str, ...] = ()


async def handle_list_work_items(data: dict[str, Any]) -> dict[str, object]:
    workspace = _workspace(data)
    work_items = await list_work_item_executions(workspace=workspace)
    return {
        "result": {
            "work_items": [work_item_execution_to_dict(item) for item in work_items],
        }
    }


async def handle_claim_work_item(data: dict[str, Any]) -> dict[str, object]:
    workspace = _workspace(data)
    issue_id = _required_str(data, "issue_id", _ISSUE_REQUIRED)
    request_id = _required_str(data, "request_id", "request_id is required")
    existing = await get_active_work_item_execution(issue_id)
    existing_response = await _existing_claim_response(existing, workspace)
    if existing_response is not None:
        return existing_response
    try:
        async with linear_client() as client:
            transition = await claim_work_item(client, workspace, issue_id, request_id)
    except WorkItemClaimConflictError as exc:
        turn = await get_in_flight_turn_for_group(workspace)
        if turn is not None and turn.turn_id == exc.execution.turn_id:
            return {"result": {"work_item": work_item_execution_to_dict(exc.execution)}}
        return {
            "error": "Linear work item is already claimed",
            "result": {"existing_execution": work_item_execution_to_dict(exc.execution)},
        }
    except (LinearError, ValueError) as exc:
        return {"error": str(exc)}
    return _execution_response(transition)


async def _existing_claim_response(
    existing: WorkItemExecution | None,
    workspace: str,
) -> dict[str, object] | None:
    if existing is None:
        return None
    if existing.workspace != workspace:
        return {"error": "Linear work item is already claimed"}
    turn = await get_in_flight_turn_for_group(workspace)
    if turn is not None and turn.turn_id == existing.turn_id:
        return {"result": {"work_item": work_item_execution_to_dict(existing)}}
    return {
        "error": "Linear work item is already claimed",
        "result": {"existing_execution": work_item_execution_to_dict(existing)},
    }


async def handle_await_review_work_item(data: dict[str, Any]) -> dict[str, object]:
    pull_request = GitHubPullRequestRef.parse(
        _required_str(data, "pull_request_url", "pull_request_url is required")
    )
    evidence_refs = tuple(dict.fromkeys((pull_request.url, *_evidence_refs(data))))
    return await _handle_linked_transition(
        data,
        _LinkedTransitionSpec(
            operation="await_review",
            target_status="awaiting_review",
            result_status=WorkItemExecutionStatus.AWAITING_REVIEW,
            expected_statuses={"in_progress", "blocked"},
        ),
        _TransitionDetails(
            summary=_required_str(data, "summary", _SUMMARY_REQUIRED),
            evidence_refs=evidence_refs,
        ),
    )


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
    async with linear_client() as client:
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


async def handle_block_work_item(data: dict[str, Any]) -> dict[str, object]:
    reason = _required_str(data, "reason", _BLOCKER_REQUIRED)
    return await _handle_linked_transition(
        data,
        _LinkedTransitionSpec(
            operation="block",
            target_status="blocked",
            result_status=WorkItemExecutionStatus.BLOCKED,
            expected_statuses={"in_progress", "awaiting_review"},
        ),
        _TransitionDetails(
            blocker=reason,
            summary=reason,
            evidence_refs=_evidence_refs(data),
        ),
    )


async def handle_handoff_work_item(data: dict[str, Any]) -> dict[str, object]:
    owner = _required_str(data, "owner", _HANDOFF_REQUIRED)
    return await _handle_linked_transition(
        data,
        _LinkedTransitionSpec(
            operation="handoff",
            target_status="blocked",
            result_status=WorkItemExecutionStatus.HANDED_OFF,
            expected_statuses={"in_progress", "awaiting_review", "blocked"},
        ),
        _TransitionDetails(
            handoff_to=owner,
            summary=_optional_str(data, "summary") or f"Handed off to {owner}",
            evidence_refs=_evidence_refs(data),
        ),
    )


async def _handle_linked_transition(
    data: dict[str, Any],
    spec: _LinkedTransitionSpec,
    details: _TransitionDetails,
) -> dict[str, object]:
    workspace = _workspace(data)
    issue_id = _required_str(data, "issue_id", _ISSUE_REQUIRED)
    request_id = _required_str(data, "request_id", "request_id is required")
    execution = await get_active_work_item_execution(issue_id)
    if execution is None or execution.workspace != workspace:
        return {"error": _ACTIVE_EXECUTION_REQUIRED}
    try:
        async with linear_client() as client:
            updated = await transition_linked_work_item(
                client,
                workspace,
                issue_id,
                WorkItemTransitionRequest(
                    execution=execution,
                    request_id=request_id,
                    operation=spec.operation,
                    target_status=spec.target_status,
                    result_execution_status=spec.result_status,
                    summary=details.summary,
                    blocker=details.blocker,
                    handoff_to=details.handoff_to,
                    evidence_refs=details.evidence_refs,
                ),
                spec.expected_statuses,
            )
    except (LinearError, ValueError) as exc:
        return {"error": str(exc)}
    return _execution_response(updated)


async def handle_reconcile_work_item(data: dict[str, Any]) -> dict[str, object]:
    workspace = _workspace(data)
    issue_id = _required_str(data, "issue_id", _ISSUE_REQUIRED)
    execution = await get_work_item_execution_for_issue(issue_id, workspace=workspace)
    if execution is None:
        return {"error": _ACTIVE_EXECUTION_REQUIRED}
    transition = await get_latest_unresolved_work_item_transition(execution.id)
    if transition is None:
        return {"error": _UNKNOWN_TRANSITION_REQUIRED}
    try:
        async with linear_client() as client:
            resolved = await reconcile_work_item(client, workspace, issue_id, transition)
    except (LinearError, ValueError) as exc:
        return {"error": str(exc)}
    return _execution_response(resolved)


async def handle_move_unlinked_todo(data: dict[str, Any]) -> dict[str, object]:
    workspace = _workspace(data)
    issue_id = _required_str(data, "issue_id", _ISSUE_REQUIRED)
    status = _required_str(data, "status", "status is required")
    active = await get_active_work_item_execution(issue_id)
    if active is not None:
        return {
            "error": "Use the linked work-item lifecycle tools while Pynchy owns this issue",
            "result": {"work_item": work_item_execution_to_dict(active)},
        }
    if status not in AGENT_SETTABLE_STATUSES:
        return {
            "error": _AGENT_SETTABLE_STATUS_ERROR.format(
                statuses=", ".join(sorted(AGENT_SETTABLE_STATUSES))
            )
        }
    try:
        async with linear_client() as client:
            updated = await move_unlinked_work_item(client, workspace, issue_id, status)
    except (LinearError, ValueError) as exc:
        return {"error": str(exc)}
    return {"result": {"issue": _issue_projection(updated)}}


def _workspace(data: dict[str, Any]) -> str:
    return _required_str(data, "source_group", _WORKSPACE_REQUIRED)


def _required_str(data: dict[str, Any], key: str, error: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(error)
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _evidence_refs(data: dict[str, Any]) -> tuple[str, ...]:
    value = data.get("evidence_refs", [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError("evidence_refs must be an array of non-empty strings")
    return tuple(value)


def work_item_execution_to_dict(execution: WorkItemExecution) -> dict[str, object]:
    """Return the safe, bounded operator projection for a work-item execution."""
    return {
        "execution_id": execution.id,
        "workspace": execution.workspace,
        "issue": {
            "id": execution.linear_issue_id,
            "identifier": execution.linear_issue_identifier,
            "url": execution.linear_issue_url,
        },
        "turn_id": execution.turn_id,
        "task_id": execution.task_id,
        "attempt": execution.attempt,
        "flow_id": execution.flow_id,
        "temporal_workflow_id": execution.temporal_workflow_id,
        "initiated_by": execution.initiated_by,
        "observed_linear_state": {
            "id": execution.observed_state_id,
            "name": execution.observed_state_name,
            "updated_at": execution.observed_updated_at,
        },
        "status": execution.status.value,
        "summary": execution.summary,
        "blocker": execution.blocker,
        "handoff_to": execution.handoff_to,
        "evidence_refs": list(execution.evidence_refs),
        "requester_delivery": {
            "status": execution.requester_delivery_status,
            "error": execution.requester_delivery_error,
            "delivered_at": execution.requester_delivered_at,
        },
        "created_at": execution.created_at,
        "updated_at": execution.updated_at,
        "completed_at": execution.completed_at,
    }


def _execution_response(execution: WorkItemExecution) -> dict[str, object]:
    """Report provider uncertainty distinctly from a completed local tool call."""
    result = {"work_item": work_item_execution_to_dict(execution)}
    if execution.status is WorkItemExecutionStatus.UNKNOWN:
        return {
            "error": (
                "Linear transition outcome is unknown; reconcile the work item before retrying"
            ),
            "result": result,
        }
    if execution.status is WorkItemExecutionStatus.FAILED:
        return {
            "error": "Linear state conflicted with the requested transition; inspect the work item",
            "result": result,
        }
    return {"result": result}


def _issue_projection(issue: dict[str, Any]) -> dict[str, object]:
    return {
        "id": issue.get("id"),
        "identifier": issue.get("identifier"),
        "url": issue.get("url"),
        "state": issue.get("state"),
        "updated_at": issue.get("updatedAt"),
    }
