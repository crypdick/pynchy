"""Host-owned lifecycle operations for Pynchy executions linked to Linear issues.

The ordinary Linear MCP server remains useful for browsing and planning.  These
operations deliberately run in the host process because a claim and its
provider-transition evidence must share Pynchy's durable state and policy
boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pynchy.host.orchestrator.workspace_config import static_workspace_folder
from pynchy.plugins.integrations.github_pull_requests import GitHubPullRequestRef
from pynchy.plugins.integrations.linear_boards import WorkspaceTodoProposal
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_statuses import (
    AGENT_SETTABLE_STATUSES,
    AWAITING_REVIEW_STATUS,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    claim_work_item,
    create_requested_work_item,
    linear_client,
    move_unlinked_work_item,
    reconcile_work_item,
    submit_unlinked_work_item_for_review,
    submit_work_item_plan,
    transition_linked_work_item,
)
from pynchy.state import (
    WorkItemClaimConflictError,
    WorkItemTransitionRequest,
    get_active_work_item_execution,
    get_in_flight_turn_for_group,
    get_latest_unresolved_work_item_transition,
    get_work_item_execution_for_issue,
    list_work_item_executions,
)
from pynchy.types import (
    InFlightTurn,
    WorkItemExecution,
    WorkItemExecutionStatus,
)

_WORKSPACE_REQUIRED = "source_group is required"
_ISSUE_REQUIRED = "issue_id is required"
_SUMMARY_REQUIRED = "summary is required"
_BLOCKER_REQUIRED = "reason is required"
_HANDOFF_REQUIRED = "owner is required"
_PLAN_REQUIRED = "plan is required"
_DIRECT_USER_TURN_REQUIRED = (
    "A current direct user turn is required to create a Ready for Planning item"
)
_AUTHORIZATION_QUOTE_REQUIRED = (
    "authorization_quote must exactly quote a current direct user message"
)
_PRIORITY_INVALID = "priority must be an integer from 0 through 4"
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


async def handle_create_requested_todo(data: dict[str, Any]) -> dict[str, object]:
    """Create planning work only when the current direct user turn requested it."""
    source_group = _source_group(data)
    workspace = static_workspace_folder(source_group)
    authorization_quote = _required_str(
        data,
        "authorization_quote",
        _AUTHORIZATION_QUOTE_REQUIRED,
    ).strip()
    # Tool wording is not proof of human intent. Bind planning authorization to
    # the host-owned turn and its complete inbound message.
    turn = await get_in_flight_turn_for_group(source_group)
    if turn is None or turn.input_source != "user":
        return {"error": _DIRECT_USER_TURN_REQUIRED}
    if not _turn_contains_authorization_quote(turn, authorization_quote):
        return {"error": _AUTHORIZATION_QUOTE_REQUIRED}

    proposal = WorkspaceTodoProposal(
        title=_required_str(data, "title", "title is required").strip(),
        description=_optional_str(data, "description"),
        priority=_optional_priority(data),
    )
    try:
        async with linear_client(workspace=workspace) as client:
            created = await create_requested_work_item(
                client,
                workspace,
                turn.chat_jid,
                proposal,
            )
    except (LinearError, ValueError) as exc:
        return {"error": str(exc)}
    return {"result": {"issue": _issue_projection(created)}}


async def handle_claim_work_item(data: dict[str, Any]) -> dict[str, object]:
    workspace = _workspace(data)
    issue_id = _required_str(data, "issue_id", _ISSUE_REQUIRED)
    request_id = _required_str(data, "request_id", "request_id is required")
    existing = await get_active_work_item_execution(issue_id)
    existing_response = await _existing_claim_response(existing, workspace)
    if existing_response is not None:
        return existing_response
    try:
        async with linear_client(workspace=workspace) as client:
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
    workspace = _workspace(data)
    issue_id = _required_str(data, "issue_id", _ISSUE_REQUIRED)
    summary = _required_str(data, "summary", _SUMMARY_REQUIRED)
    evidence_refs = _review_evidence_refs(data)
    execution = await get_active_work_item_execution(issue_id)

    # Awaiting Review describes a completed outcome awaiting acceptance, not a
    # GitHub artifact. Non-code and already-completed work may have no PR or claim.
    if execution is None:
        try:
            async with linear_client(workspace=workspace) as client:
                updated = await submit_unlinked_work_item_for_review(client, workspace, issue_id)
        except (LinearError, ValueError) as exc:
            return {"error": str(exc)}
        return {
            "result": {
                "issue": _issue_projection(updated),
                "review": {"summary": summary, "evidence_refs": list(evidence_refs)},
            }
        }
    if execution.workspace != workspace:
        return {"error": _ACTIVE_EXECUTION_REQUIRED}
    return await _handle_linked_transition(
        data,
        _LinkedTransitionSpec(
            operation="await_review",
            target_status=AWAITING_REVIEW_STATUS,
            result_status=WorkItemExecutionStatus.AWAITING_REVIEW,
            expected_statuses={"in_progress", "blocked"},
        ),
        _TransitionDetails(
            summary=summary,
            evidence_refs=evidence_refs,
        ),
    )


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
        async with linear_client(workspace=workspace) as client:
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
        async with linear_client(workspace=workspace) as client:
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
        async with linear_client(workspace=workspace) as client:
            updated = await move_unlinked_work_item(client, workspace, issue_id, status)
    except (LinearError, ValueError) as exc:
        return {"error": str(exc)}
    return {"result": {"issue": _issue_projection(updated)}}


async def handle_submit_plan(data: dict[str, Any]) -> dict[str, object]:
    workspace = _workspace(data)
    issue_id = _required_str(data, "issue_id", _ISSUE_REQUIRED)
    plan = _required_str(data, "plan", _PLAN_REQUIRED)
    active = await get_active_work_item_execution(issue_id)
    if active is not None:
        return {
            "error": "A claimed Linear work item cannot re-enter planning",
            "result": {"work_item": work_item_execution_to_dict(active)},
        }
    try:
        async with linear_client(workspace=workspace) as client:
            updated = await submit_work_item_plan(client, workspace, issue_id, plan)
    except (LinearError, ValueError) as exc:
        return {"error": str(exc)}
    return {"result": {"issue": _issue_projection(updated)}}


def _workspace(data: dict[str, Any]) -> str:
    return static_workspace_folder(_source_group(data))


def _source_group(data: dict[str, Any]) -> str:
    return _required_str(data, "source_group", _WORKSPACE_REQUIRED)


def _required_str(data: dict[str, Any], key: str, error: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(error)
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_priority(data: dict[str, Any]) -> int | None:
    value = data.get("priority")
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 4:
        raise ValueError(_PRIORITY_INVALID)
    return value


def _turn_contains_authorization_quote(turn: InFlightTurn, quote: str) -> bool:
    return any(
        message.get("message_type") == "user"
        and isinstance(content := message.get("content"), str)
        and quote == content.strip()
        for message in turn.input_messages
    )


def _evidence_refs(data: dict[str, Any]) -> tuple[str, ...]:
    value = data.get("evidence_refs", [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError("evidence_refs must be an array of non-empty strings")
    return tuple(value)


def _review_evidence_refs(data: dict[str, Any]) -> tuple[str, ...]:
    evidence_refs = _evidence_refs(data)
    pull_request_url = _optional_str(data, "pull_request_url")
    if pull_request_url is None:
        return tuple(dict.fromkeys(evidence_refs))
    pull_request = GitHubPullRequestRef.parse(pull_request_url)
    return tuple(dict.fromkeys((pull_request.url, *evidence_refs)))


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
