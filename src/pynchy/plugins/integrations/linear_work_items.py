"""Host-enforced boundaries for agent-managed Linear work."""

from __future__ import annotations

import re
from collections.abc import (  # noqa: TC003 - beartype resolves configured work-item callbacks at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from typing import Any

from pynchy.conversation.api import parent_workspace_name
from pynchy.plugins.integrations.linear_client import LinearClient, LinearError
from pynchy.plugins.integrations.linear_statuses import (
    HUMAN_SETTABLE_STATUSES,
    TERMINAL_STATE_TYPES,
    TOOL_SETTABLE_STATUSES,
)
from pynchy.plugins.integrations.linear_work_item_planning import submit_work_item_plan
from pynchy.plugins.integrations.linear_work_item_provider import (
    linear_client,
    reconcile_work_item,
    state_id,
    transition_linked_work_item,
    update_issue_state,
    workspace_issue,
)
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransitionRequest,
)

_WORKSPACE_REQUIRED = "source_group is required"
_ISSUE_REQUIRED = "issue_id is required"
_ACTIVE_EXECUTION_REQUIRED = "No Pynchy execution owns this Linear work item"
_UNKNOWN_TRANSITION_REQUIRED = "No uncertain work-item transition needs reconciliation"
_PLAN_REQUIRED = "plan is required"
_DIRECT_USER_REQUIRED = "Human Approved and Rejected require a current direct-human instruction"
_TERMINAL_USER_REQUIRED = "Only a current direct-human instruction can reopen terminal work"
_HOST_MANAGED_STATUS = "In Progress is managed by the host execution lease"
_GITHUB_PULL_REQUEST_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?"
)


@dataclass(frozen=True)
class _LinkedMove:
    target_status: str
    result_status: WorkItemExecutionStatus
    expected_statuses: set[str]


@dataclass(frozen=True)
class _MoveOutcome:
    """Typed durable evidence supplied with one generic lifecycle move."""

    summary: str | None
    blocker: str | None
    handoff_to: str | None
    evidence_refs: tuple[str, ...] | None


async def _attach_github_pull_request_evidence(
    client: LinearClient,
    issue_id: str,
    target_status: str,
    evidence_refs: tuple[str, ...] | None,
) -> None:
    if target_status != "awaiting_review":
        return
    for evidence_ref in evidence_refs or ():
        match = _GITHUB_PULL_REQUEST_URL.fullmatch(evidence_ref)
        if match is not None:
            owner, repository, number = match.groups()
            await client.create_attachment(
                issue_id,
                f"https://github.com/{owner}/{repository}/pull/{number}",
                f"{owner}/{repository} #{number}",
            )


async def attach_work_item_pull_request(
    workspace: str,
    issue_id: str,
    repository: str,
    pr_url: str,
) -> str | None:
    """Attach one host-validated publication to its exact Linear execution."""
    try:
        async with linear_client(workspace=workspace) as client:
            number = pr_url.rsplit("/", maxsplit=1)[-1]
            await client.create_attachment(issue_id, pr_url, f"{repository} #{number}")
    except (LinearError, ValueError) as exc:
        return str(exc)
    return None


@dataclass(frozen=True)
class LinearWorkItemsRuntime:
    """Durable work-item queries and bindings selected during plugin composition."""

    list_executions: Callable[..., Awaitable[list[WorkItemExecution]]]
    get_active_execution: Callable[[str], Awaitable[WorkItemExecution | None]]
    get_execution_for_issue: Callable[..., Awaitable[WorkItemExecution | None]]
    get_in_flight_turn: Callable[[str], Awaitable[Any]]
    bind_execution_to_turn: Callable[..., Awaitable[WorkItemExecution]]
    get_latest_reconcilable_transition: Callable[[str], Awaitable[Any]]


@dataclass
class _RuntimeState:
    runtime: LinearWorkItemsRuntime | None = None


_runtime = _RuntimeState()


def configure_linear_work_items_runtime(runtime: LinearWorkItemsRuntime) -> None:
    """Set durable operations used by host-facing Linear work-item actions."""
    _runtime.runtime = runtime


def _configured_runtime() -> LinearWorkItemsRuntime:
    if _runtime.runtime is None:
        raise RuntimeError("Linear work-items runtime has not been configured")
    return _runtime.runtime


_LINKED_MOVES = {
    "awaiting_review": _LinkedMove(
        target_status="awaiting_review",
        result_status=WorkItemExecutionStatus.AWAITING_REVIEW,
        expected_statuses={"in_progress", "blocked"},
    ),
    "blocked": _LinkedMove(
        target_status="blocked",
        result_status=WorkItemExecutionStatus.BLOCKED,
        expected_statuses={"in_progress", "awaiting_review", "follow_ups", "blocked"},
    ),
    "follow_ups": _LinkedMove(
        target_status="follow_ups",
        result_status=WorkItemExecutionStatus.FOLLOW_UPS,
        expected_statuses={"in_progress", "awaiting_review", "follow_ups", "blocked"},
    ),
    "done": _LinkedMove(
        target_status="done",
        result_status=WorkItemExecutionStatus.COMPLETED,
        expected_statuses={"in_progress", "awaiting_review", "follow_ups", "blocked", "done"},
    ),
    "rejected": _LinkedMove(
        target_status="rejected",
        result_status=WorkItemExecutionStatus.CANCELLED,
        expected_statuses={"in_progress", "awaiting_review", "follow_ups", "blocked", "rejected"},
    ),
}


async def handle_list_work_items(data: dict[str, Any]) -> dict[str, object]:
    workspace = _workspace(data)
    work_items = await _configured_runtime().list_executions(workspace=workspace)
    return {
        "result": {
            "work_items": [work_item_execution_to_dict(item) for item in work_items],
        }
    }


async def handle_move_todo(data: dict[str, Any]) -> dict[str, object]:
    """Move an issue while enforcing only authority and execution ownership."""
    source_group = _source_group(data)
    workspace = _workspace_folder(source_group)
    issue_id = _required_str(data, "issue_id", _ISSUE_REQUIRED)
    status = _required_str(data, "status", "status is required")
    direct_user = await _has_direct_user_turn(source_group)
    if error := _move_request_error(status, direct_user=direct_user):
        return {"error": error}

    runtime = _configured_runtime()
    active = await runtime.get_active_execution(issue_id)
    if active is not None and active.workspace != workspace:
        return {"error": _ACTIVE_EXECUTION_REQUIRED}
    latest = active or await runtime.get_execution_for_issue(issue_id, workspace=workspace)
    move = _LINKED_MOVES.get(status)
    if latest is not None and move is not None and _move_applies_to_execution(latest, status):
        try:
            outcome = _linked_move_outcome(data, status)
        except (TypeError, ValueError) as exc:
            return {"error": str(exc)}
        return await _move_linked(data, latest, move, source_group, outcome)
    if active is not None:
        return {
            "error": (
                "The active execution must move to Awaiting Review, Follow-ups, Blocked, Done, "
                "or Rejected"
            ),
            "result": {"work_item": work_item_execution_to_dict(active)},
        }
    return await _move_provider_issue(
        workspace,
        issue_id,
        status,
        direct_user=direct_user,
    )


async def handle_submit_plan(data: dict[str, Any]) -> dict[str, object]:
    """Persist a plan without granting or beginning execution."""
    workspace = _workspace(data)
    issue_id = _required_str(data, "issue_id", _ISSUE_REQUIRED)
    plan = _required_str(data, "plan", _PLAN_REQUIRED)
    active = await _configured_runtime().get_active_execution(issue_id)
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


def _move_request_error(status: str, *, direct_user: bool) -> str | None:
    if status == "in_progress":
        return _HOST_MANAGED_STATUS
    if status not in TOOL_SETTABLE_STATUSES:
        return _status_error()
    if status in HUMAN_SETTABLE_STATUSES and not direct_user:
        return _DIRECT_USER_REQUIRED
    return None


def _move_applies_to_execution(execution: WorkItemExecution, status: str) -> bool:
    if execution.status.is_active:
        return True
    return status in _LINKED_MOVES and execution.status in {
        WorkItemExecutionStatus.AWAITING_REVIEW,
        WorkItemExecutionStatus.FOLLOW_UPS,
        WorkItemExecutionStatus.BLOCKED,
    }


async def _move_linked(
    data: dict[str, Any],
    execution: WorkItemExecution,
    move: _LinkedMove,
    source_group: str,
    outcome: _MoveOutcome,
) -> dict[str, object]:
    request_id = _required_str(data, "request_id", "request_id is required")
    runtime = _configured_runtime()
    turn = await runtime.get_in_flight_turn(source_group)
    if turn is None:
        return {"error": "A current agent turn is required to report a linked outcome"}
    try:
        if execution.status.is_active:
            execution = await runtime.bind_execution_to_turn(
                execution.id,
                turn_id=turn.turn_id,
                task_id=turn.task_id,
            )
        async with linear_client(workspace=execution.workspace) as client:
            await _attach_github_pull_request_evidence(
                client,
                execution.linear_issue_id,
                move.target_status,
                outcome.evidence_refs
                if outcome.evidence_refs is not None
                else execution.evidence_refs,
            )
            updated = await transition_linked_work_item(
                client,
                execution.workspace,
                execution.linear_issue_id,
                WorkItemTransitionRequest(
                    execution=execution,
                    request_id=request_id,
                    operation=f"move_to_{move.target_status}",
                    target_status=move.target_status,
                    result_execution_status=move.result_status,
                    summary=outcome.summary,
                    blocker=outcome.blocker,
                    handoff_to=outcome.handoff_to,
                    evidence_refs=outcome.evidence_refs,
                    requester_delivery_turn_id=turn.turn_id,
                ),
                move.expected_statuses,
            )
    except (LinearError, ValueError) as exc:
        return {"error": str(exc)}
    return _execution_response(updated)


async def _move_provider_issue(
    workspace: str,
    issue_id: str,
    status: str,
    *,
    direct_user: bool,
) -> dict[str, object]:
    try:
        updated = await _apply_provider_move(
            workspace,
            issue_id,
            status,
            direct_user=direct_user,
        )
    except (LinearError, TypeError, ValueError) as exc:
        return {"error": str(exc)}
    return {"result": {"issue": _issue_projection(updated)}}


async def _apply_provider_move(
    workspace: str,
    issue_id: str,
    status: str,
    *,
    direct_user: bool,
) -> dict[str, Any]:
    async with linear_client(workspace=workspace) as client:
        issue, board = await workspace_issue(client, workspace, issue_id)
        current_state = issue.get("state")
        if not isinstance(current_state, dict):
            raise TypeError("Linear issue state was not an object")
        if current_state.get("type") in TERMINAL_STATE_TYPES and not direct_user:
            raise ValueError(_TERMINAL_USER_REQUIRED)
        return await update_issue_state(
            client,
            issue_id,
            state_id(board.states[status]),
        )


async def _has_direct_user_turn(source_group: str) -> bool:
    turn = await _configured_runtime().get_in_flight_turn(source_group)
    return turn is not None and turn.input_source == "user"


async def handle_reconcile_work_item(data: dict[str, Any]) -> dict[str, object]:
    workspace = _workspace(data)
    issue_id = _required_str(data, "issue_id", _ISSUE_REQUIRED)
    runtime = _configured_runtime()
    execution = await runtime.get_execution_for_issue(issue_id, workspace=workspace)
    if execution is None:
        return {"error": _ACTIVE_EXECUTION_REQUIRED}
    transition = await runtime.get_latest_reconcilable_transition(execution.id)
    if transition is None:
        return {"error": _UNKNOWN_TRANSITION_REQUIRED}
    try:
        async with linear_client(workspace=workspace) as client:
            resolved = await reconcile_work_item(client, workspace, issue_id, transition)
    except (LinearError, ValueError) as exc:
        return {"error": str(exc)}
    return _execution_response(resolved)


def _workspace(data: dict[str, Any]) -> str:
    return _workspace_folder(_source_group(data))


def _workspace_folder(folder: str) -> str:
    return parent_workspace_name(folder) or folder


def _source_group(data: dict[str, Any]) -> str:
    return _required_str(data, "source_group", _WORKSPACE_REQUIRED)


def _required_str(data: dict[str, Any], key: str, error: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(error)
    return value


def _linked_move_outcome(data: dict[str, Any], status: str) -> _MoveOutcome:
    raw = data.get("outcome")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TypeError("outcome must be an object")
    unexpected = set(raw) - {"summary", "blocker", "handoff_to", "evidence_refs"}
    if unexpected:
        raise ValueError(f"outcome contains unsupported fields: {', '.join(sorted(unexpected))}")

    summary = _outcome_text(raw, "summary")
    blocker = _outcome_text(raw, "blocker")
    handoff_to = _outcome_text(raw, "handoff_to")
    evidence_refs = _outcome_evidence_refs(raw)
    if status == "blocked":
        if blocker is None:
            raise ValueError("outcome.blocker is required when moving work to Blocked")
        summary = summary or blocker
    elif status in {"awaiting_review", "follow_ups", "done"} and summary is None:
        raise ValueError(f"outcome.summary is required when moving work to {status}")
    if status != "blocked" and (blocker is not None or handoff_to is not None):
        raise ValueError("outcome.blocker and outcome.handoff_to are only valid for Blocked work")
    return _MoveOutcome(summary, blocker, handoff_to, evidence_refs)


def _outcome_text(outcome: dict[str, object], key: str) -> str | None:
    value = outcome.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"outcome.{key} must be text")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"outcome.{key} must not be empty")
    return stripped


def _outcome_evidence_refs(outcome: dict[str, object]) -> tuple[str, ...] | None:
    if "evidence_refs" not in outcome:
        return None
    value = outcome["evidence_refs"]
    if not isinstance(value, list):
        raise TypeError("outcome.evidence_refs must be an array")
    refs: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("outcome.evidence_refs must contain non-empty strings")
        refs.append(item.strip())
    return tuple(dict.fromkeys(refs))


def _status_error() -> str:
    return "status must be one of: " + ", ".join(sorted(TOOL_SETTABLE_STATUSES))


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
            "turn_id": execution.requester_delivery_turn_id,
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
