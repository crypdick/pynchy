"""Provider I/O and reconciliation primitives for Linear work-item lifecycle actions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import aiohttp

from pynchy.logger import logger
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard, require_workspace_board
from pynchy.plugins.integrations.linear_client import LinearClient, LinearError
from pynchy.plugins.integrations.linear_plans import description_with_plan, update_issue_plan
from pynchy.plugins.integrations.linear_statuses import (
    AWAITING_PLAN_APPROVAL_STATUS,
    HUMAN_APPROVED_STATUS,
    READY_FOR_PLANNING_STATUS,
)
from pynchy.state import (
    WorkItemClaimRequest,
    WorkItemTransitionRequest,
    begin_work_item_transition,
    create_work_item_claim,
    get_in_flight_turn_for_group,
    get_work_item_transition_by_request,
    resolve_work_item_transition,
)
from pynchy.types import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransition,
    WorkItemTransitionStatus,
)

_WORKSPACE_ISSUE_REQUIRED = "Linear issue does not belong to this Pynchy workspace board"
_HUMAN_APPROVAL_REQUIRED = "Linear work item must be Human Approved before Pynchy can claim it"
_PLANNING_READY_REQUIRED = "Linear work item must be Ready for Planning before planning"


class LinearWorkspaceIssueError(ValueError):
    """The requested issue cannot participate in this workspace's Linear workflow."""


@dataclass(frozen=True)
class _WorkspaceContext:
    """Minimal board identity; the folder is Pynchy's canonical workspace key."""

    folder: str
    name: str
    jid: str = ""


@dataclass(frozen=True)
class _TransitionAttempt:
    """All local and provider state needed to apply one transition."""

    board: LinearWorkspaceBoard
    execution: WorkItemExecution
    transition: WorkItemTransition
    expected_statuses: set[str]
    target_status: str


class LinearClientContext:
    """Own the aiohttp session needed by a short-lived host Linear operation."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> LinearClient:
        api_key = os.environ.get("LINEAR_API_KEY")
        if not api_key:
            raise ValueError("LINEAR_API_KEY is not configured")  # pragma: allowlist secret
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return LinearClient(api_key=api_key, session=self._session)

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if self._session is not None:
            await self._session.close()


def linear_client() -> LinearClientContext:
    """Create a context manager for one host-owned Linear operation."""
    return LinearClientContext()


async def claim_work_item(
    client: LinearClient,
    workspace: str,
    issue_id: str,
    request_id: str,
) -> WorkItemExecution:
    """Persist a claim, then transition a Human Approved issue to In Progress."""
    issue, board = await workspace_issue(client, workspace, issue_id)
    if state_id(issue) != state_id(board.states[HUMAN_APPROVED_STATUS]):
        raise ValueError(_HUMAN_APPROVAL_REQUIRED)
    turn = await get_in_flight_turn_for_group(workspace)
    execution = await create_work_item_claim(
        WorkItemClaimRequest(
            workspace=workspace,
            issue=issue,
            turn_id=turn.turn_id if turn else None,
            task_id=turn.task_id if turn else None,
            initiated_by=turn.chat_jid if turn else workspace,
            request_id=request_id,
        )
    )
    transition = await _pending_transition(execution.id, request_id)
    return await transition_issue(
        client,
        _TransitionAttempt(
            board=board,
            execution=execution,
            transition=transition,
            expected_statuses={HUMAN_APPROVED_STATUS},
            target_status="in_progress",
        ),
    )


async def transition_linked_work_item(
    client: LinearClient,
    workspace: str,
    issue_id: str,
    request: WorkItemTransitionRequest,
    expected_statuses: set[str],
) -> WorkItemExecution:
    """Persist a transition intent, then apply it against the latest Linear state."""
    _issue, board = await workspace_issue(client, workspace, issue_id)
    transition = await begin_work_item_transition(request)
    return await transition_issue(
        client,
        _TransitionAttempt(
            board=board,
            execution=request.execution,
            transition=transition,
            expected_statuses=expected_statuses,
            target_status=request.target_status,
        ),
    )


async def reconcile_work_item(
    client: LinearClient,
    workspace: str,
    issue_id: str,
    transition: WorkItemTransition,
) -> WorkItemExecution:
    """Resolve an unknown provider receipt from Linear's observed current state."""
    issue, board = await workspace_issue(client, workspace, issue_id)
    matches_target = state_id(issue) == state_id(board.states[transition.target_status])
    return await resolve_work_item_transition(
        transition=transition,
        execution_status=(
            transition.result_execution_status if matches_target else WorkItemExecutionStatus.FAILED
        ),
        transition_status=(
            WorkItemTransitionStatus.SUCCEEDED
            if matches_target
            else WorkItemTransitionStatus.CONFLICT
        ),
        issue=issue,
        error=None if matches_target else "Linear state differs from the intended transition",
    )


async def move_unlinked_work_item(
    client: LinearClient,
    workspace: str,
    issue_id: str,
    status: str,
) -> dict[str, Any]:
    """Move a board item only after the caller established it has no active claim."""
    _issue, board = await workspace_issue(client, workspace, issue_id)
    if status not in board.states:
        raise ValueError(f"Unknown Pynchy todo status: {status}")
    return await update_issue_state(client, issue_id, state_id(board.states[status]))


async def submit_work_item_plan(
    client: LinearClient,
    workspace: str,
    issue_id: str,
    plan: str,
) -> dict[str, Any]:
    """Persist a concrete plan before advancing to the human plan-approval gate."""
    issue, board = await workspace_issue(client, workspace, issue_id)
    if state_id(issue) != state_id(board.states[READY_FOR_PLANNING_STATUS]):
        raise ValueError(_PLANNING_READY_REQUIRED)
    description = description_with_plan(issue.get("description"), plan)
    return await update_issue_plan(
        client,
        issue_id=issue_id,
        state_id=state_id(board.states[AWAITING_PLAN_APPROVAL_STATUS]),
        description=description,
    )


async def transition_issue(
    client: LinearClient,
    attempt: _TransitionAttempt,
) -> WorkItemExecution:
    """Check Linear immediately before writing; uncertain writes remain explicitly unknown."""
    try:
        outcome = await _apply_transition(client, attempt)
    except Exception as exc:  # noqa: BLE001, RUF100 - provider errors can leave a write ambiguous.
        logger.warning("Linear work-item transition outcome is unknown", err=str(exc))
        return await resolve_work_item_transition(
            transition=attempt.transition,
            execution_status=WorkItemExecutionStatus.UNKNOWN,
            transition_status=WorkItemTransitionStatus.UNKNOWN,
            error=f"Linear transition outcome is unknown: {exc}",
        )
    if isinstance(outcome, WorkItemExecution):
        return outcome
    return await resolve_work_item_transition(
        transition=attempt.transition,
        execution_status=attempt.transition.result_execution_status,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
        issue=outcome,
    )


async def _apply_transition(
    client: LinearClient,
    attempt: _TransitionAttempt,
) -> dict[str, Any] | WorkItemExecution:
    """Perform the conditional provider write or record a confirmed state conflict."""
    current = await client.get_issue(attempt.execution.linear_issue_id)
    if current is None:
        raise ValueError("Linear issue no longer exists")
    expected_state_ids = {state_id(attempt.board.states[key]) for key in attempt.expected_statuses}
    if state_id(current) not in expected_state_ids:
        return await resolve_work_item_transition(
            transition=attempt.transition,
            execution_status=WorkItemExecutionStatus.FAILED,
            transition_status=WorkItemTransitionStatus.CONFLICT,
            issue=current,
            error="Linear state changed before Pynchy could apply the intended transition",
        )
    return await update_issue_state(
        client,
        attempt.execution.linear_issue_id,
        state_id(attempt.board.states[attempt.target_status]),
    )


async def workspace_issue(
    client: LinearClient,
    workspace: str,
    issue_id: str,
) -> tuple[dict[str, Any], LinearWorkspaceBoard]:
    """Load a board issue while enforcing its workspace-project ownership."""
    board = await require_workspace_board(
        client,
        _WorkspaceContext(folder=workspace, name=_workspace_name(workspace)),
        team_key=os.environ.get("LINEAR_TEAM_KEY"),
    )
    issue = await client.get_issue(issue_id)
    if issue is None:
        raise LinearWorkspaceIssueError("Linear issue does not exist")
    project = issue.get("project")
    if not isinstance(project, dict) or project.get("id") != board.project.get("id"):
        raise LinearWorkspaceIssueError(_WORKSPACE_ISSUE_REQUIRED)
    return issue, board


async def update_issue_state(
    client: LinearClient,
    issue_id: str,
    state_id: str,
) -> dict[str, Any]:
    """Apply one GraphQL issue-state update and require a provider receipt."""
    data = await client.query(
        """
        mutation TransitionPynchyWorkItem($issue_id: String!, $state_id: String!) {
          issueUpdate(id: $issue_id, input: { stateId: $state_id }) {
            success
            issue {
              id identifier title url updatedAt
              state { id name type }
              project { id name }
            }
          }
        }
        """,
        issue_id=issue_id,
        state_id=state_id,
    )
    result = data.get("issueUpdate")
    if not isinstance(result, dict) or not result.get("success"):
        raise LinearError("Linear did not update the work item")
    issue = result.get("issue")
    if not isinstance(issue, dict):
        raise LinearError("Linear work-item update response did not include an issue")
    return issue


async def _pending_transition(execution_id: str, request_id: str) -> WorkItemTransition:
    transition = await get_work_item_transition_by_request(request_id)
    if transition is None or transition.execution_id != execution_id:
        raise RuntimeError("work item claim transition is missing")
    return transition


def state_id(payload: dict[str, Any]) -> str:
    """Extract an ID from either an issue payload or a workflow-state payload."""
    state = payload.get("state", payload)
    if not isinstance(state, dict):
        raise TypeError("Linear issue payload missing state")
    value = state.get("id")
    if not isinstance(value, str) or not value:
        raise ValueError("Linear issue state missing id")
    return value


def _workspace_name(folder: str) -> str:
    return folder.replace("-", " ").replace("_", " ").title()
