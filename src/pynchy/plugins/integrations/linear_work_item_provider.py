"""Provider I/O and reconciliation primitives for Linear work-item lifecycle actions."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves configured work-item callbacks at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from typing import Any, cast, overload

import aiohttp

from pynchy.conversation.api import (  # noqa: TC001 - beartype resolves annotations.
    ConversationLifecycleFence,
)
from pynchy.logger import logger
from pynchy.plugins.integrations.linear_accounts import (
    LinearAccount,
    linear_account,
    linear_account_for_workspace,
)
from pynchy.plugins.integrations.linear_boards import (
    LinearWorkspaceBoard,
    require_workspace_board,
)
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_issue_mutations import (
    update_issue_state,
)
from pynchy.plugins.integrations.linear_self_echoes import (
    linear_self_echo_recorder,
)
from pynchy.plugins.integrations.linear_statuses import (
    HUMAN_APPROVED_STATUS,
)
from pynchy.work_items.api import (
    WorkItemClaimConflictError,
    WorkItemClaimRequest,
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransition,
    WorkItemTransitionRequest,
    WorkItemTransitionResolution,
    WorkItemTransitionStatus,
)

_WORKSPACE_ISSUE_REQUIRED = "Linear issue does not belong to this Pynchy workspace board"
_HUMAN_APPROVAL_REQUIRED = "Linear work item must be Human Approved before Pynchy can run it"


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


@dataclass(frozen=True)
class WorkItemLeaseRequest:
    """Host-derived authority and ownership for one execution lease."""

    workspace: str
    issue_id: str
    request_id: str
    initiated_by: str
    turn_id: str | None = None
    task_id: str | None = None
    board: LinearWorkspaceBoard | None = None


@dataclass(frozen=True)
class LinearWorkItemRuntime:
    """Durable work-item operations selected during Linear plugin composition."""

    get_transition_by_request: Callable[[str], Awaitable[WorkItemTransition | None]]
    get_execution: Callable[[str], Awaitable[WorkItemExecution | None]]
    get_active_execution: Callable[[str], Awaitable[WorkItemExecution | None]]
    create_claim: Callable[[WorkItemClaimRequest], Awaitable[WorkItemExecution]]
    begin_transition: Callable[[WorkItemTransitionRequest], Awaitable[WorkItemTransition]]
    resolve_transition: Callable[..., Awaitable[WorkItemExecution]]
    resolve_transition_if_lifecycle_current: Callable[..., Awaitable[WorkItemExecution | None]]


_runtime: LinearWorkItemRuntime | None = None


def configure_linear_work_item_runtime(runtime: LinearWorkItemRuntime) -> None:
    """Set durable work-item operations for Linear provider reconciliation."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> LinearWorkItemRuntime:
    if _runtime is None:
        raise RuntimeError("Linear work-item runtime has not been configured")
    return _runtime


class LinearClientContext:
    """Own the aiohttp session needed by a short-lived host Linear operation."""

    def __init__(self, account: LinearAccount) -> None:
        self._account = account
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> LinearClient:
        api_key = self._account.api_key
        if not api_key:
            raise ValueError(
                f"{self._account.config.api_key_env} is not configured"
            )  # pragma: allowlist secret
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return LinearClient(
            api_key=api_key,
            session=self._session,
            team_key=self._account.team_key,
            self_echo_recorder=linear_self_echo_recorder(self._account.name),
        )

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await cast("aiohttp.ClientSession", self._session).close()


def linear_client(
    *,
    workspace: str | None = None,
    account_name: str | None = None,
) -> LinearClientContext:
    """Create a client for one exact workspace-selected or named account."""
    if (workspace is None) == (account_name is None):
        raise ValueError("Linear client requires exactly one workspace or account name")
    account: LinearAccount | None
    if account_name is not None:
        account = linear_account(account_name)
    elif workspace is not None:
        account = linear_account_for_workspace(workspace)
    else:
        raise AssertionError("Linear account selector validation failed")
    if account is None:
        raise ValueError(f"Workspace '{workspace}' does not select a Linear account")
    return LinearClientContext(account)


async def acquire_work_item_lease(
    client: LinearClient,
    request: WorkItemLeaseRequest,
) -> WorkItemExecution:
    """Acquire one durable execution lease before agent work begins."""
    return await _acquire_work_item_lease(
        client,
        request,
        admitted_status=HUMAN_APPROVED_STATUS,
        admission_error=_HUMAN_APPROVAL_REQUIRED,
    )


async def acquire_human_started_work_item_lease(
    client: LinearClient,
    request: WorkItemLeaseRequest,
) -> WorkItemExecution:
    """Adopt a human-started provider issue into the durable execution controller.

    The caller must derive authority from an authenticated webhook whose user
    actor changed the issue state. This provider primitive deliberately cannot
    infer that provenance from Linear's current state alone.
    """
    return await _acquire_work_item_lease(
        client,
        request,
        admitted_status="in_progress",
        admission_error="Linear work item must be human-started before Pynchy can adopt it",
    )


async def _acquire_work_item_lease(
    client: LinearClient,
    request: WorkItemLeaseRequest,
    *,
    admitted_status: str,
    admission_error: str,
) -> WorkItemExecution:
    runtime = _configured_runtime()
    prior_transition = await runtime.get_transition_by_request(request.request_id)
    if prior_transition is not None:
        execution = await runtime.get_execution(prior_transition.execution_id)
        if execution is None:
            raise RuntimeError("work item lease transition lost its execution")
        if (
            execution.workspace != request.workspace
            or execution.linear_issue_id != request.issue_id
        ):
            raise ValueError("work item lease request_id belongs to another execution")
        return await _resume_work_item_lease(
            client,
            request,
            execution,
            prior_transition,
        )

    existing = await runtime.get_active_execution(request.issue_id)
    if existing is not None:
        raise WorkItemClaimConflictError(existing)

    issue, board = await _lease_issue_context(client, request)
    if state_id(issue) != state_id(board.states[admitted_status]):
        raise ValueError(admission_error)
    try:
        execution = await runtime.create_claim(
            WorkItemClaimRequest(
                workspace=request.workspace,
                issue=issue,
                turn_id=request.turn_id,
                task_id=request.task_id,
                initiated_by=request.initiated_by,
                request_id=request.request_id,
            )
        )
    except WorkItemClaimConflictError as exc:
        transition = await runtime.get_transition_by_request(request.request_id)
        if transition is None or transition.execution_id != exc.execution.id:
            raise
        return await _resume_work_item_lease(
            client,
            request,
            exc.execution,
            transition,
        )
    transition = await _pending_transition(execution.id, request.request_id)
    if admitted_status == "in_progress":
        return await runtime.resolve_transition(
            transition=transition,
            execution_status=WorkItemExecutionStatus.IN_PROGRESS,
            transition_status=WorkItemTransitionStatus.SUCCEEDED,
            issue=issue,
        )
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


async def _lease_issue_context(
    client: LinearClient,
    request: WorkItemLeaseRequest,
) -> tuple[dict[str, Any], LinearWorkspaceBoard]:
    if request.board is None:
        return await workspace_issue(client, request.workspace, request.issue_id)
    issue = await client.get_issue(request.issue_id)
    if issue is None:
        raise LinearWorkspaceIssueError("Linear issue does not exist")
    project = issue.get("project")
    if not isinstance(project, dict) or project.get("id") != request.board.project.get("id"):
        raise LinearWorkspaceIssueError(_WORKSPACE_ISSUE_REQUIRED)
    return issue, request.board


async def _resume_work_item_lease(
    client: LinearClient,
    request: WorkItemLeaseRequest,
    execution: WorkItemExecution,
    transition: WorkItemTransition,
) -> WorkItemExecution:
    """Finish or reconcile an interrupted host-owned lease acquisition."""
    if transition.status in {
        WorkItemTransitionStatus.SUCCEEDED,
        WorkItemTransitionStatus.CONFLICT,
    }:
        return execution
    issue, board = await _lease_issue_context(client, request)
    current_state_id = state_id(issue)
    if current_state_id == state_id(board.states["in_progress"]):
        return await _configured_runtime().resolve_transition(
            transition=transition,
            execution_status=WorkItemExecutionStatus.IN_PROGRESS,
            transition_status=WorkItemTransitionStatus.SUCCEEDED,
            issue=issue,
        )
    if current_state_id != state_id(board.states[HUMAN_APPROVED_STATUS]):
        return await _configured_runtime().resolve_transition(
            transition=transition,
            execution_status=WorkItemExecutionStatus.FAILED,
            transition_status=WorkItemTransitionStatus.CONFLICT,
            issue=issue,
            error="Linear state differs from the intended execution lease",
        )
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
    transition = await _configured_runtime().begin_transition(request)
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


@overload
async def reconcile_work_item(
    client: LinearClient,
    workspace: str,
    issue_id: str,
    transition: WorkItemTransition,
    *,
    lifecycle_fence: None = None,
) -> WorkItemExecution: ...


@overload
async def reconcile_work_item(
    client: LinearClient,
    workspace: str,
    issue_id: str,
    transition: WorkItemTransition,
    *,
    lifecycle_fence: ConversationLifecycleFence,
) -> WorkItemExecution | None: ...


async def reconcile_work_item(
    client: LinearClient,
    workspace: str,
    issue_id: str,
    transition: WorkItemTransition,
    *,
    lifecycle_fence: ConversationLifecycleFence | None = None,
) -> WorkItemExecution | None:
    """Resolve an unknown provider receipt from Linear's observed current state."""
    issue, board = await workspace_issue(client, workspace, issue_id)
    matches_target = state_id(issue) == state_id(board.states[transition.target_status])
    runtime = _configured_runtime()
    if (
        not matches_target
        and transition.operation == "claim"
        and transition.status is WorkItemTransitionStatus.UNKNOWN
        and state_id(issue) == state_id(board.states[HUMAN_APPROVED_STATUS])
    ):
        # Human Approved proves an uncertain claim write did not land.
        execution = await runtime.get_execution(transition.execution_id)
        if execution is None:
            raise RuntimeError("work item transition lost its execution")
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
    resolution = WorkItemTransitionResolution(
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
    if lifecycle_fence is not None:
        return await runtime.resolve_transition_if_lifecycle_current(
            resolution,
            lifecycle_fence=lifecycle_fence,
        )
    return await runtime.resolve_transition(
        transition=resolution.transition,
        execution_status=resolution.execution_status,
        transition_status=resolution.transition_status,
        issue=resolution.issue,
        error=resolution.error,
    )


async def transition_issue(
    client: LinearClient,
    attempt: _TransitionAttempt,
) -> WorkItemExecution:
    """Check Linear immediately before writing; uncertain writes remain explicitly unknown."""
    try:
        outcome = await _apply_transition(client, attempt)
    except Exception as exc:  # noqa: BLE001 - provider errors can leave a write ambiguous.
        logger.warning("Linear work-item transition outcome is unknown", err=str(exc))
        return await _configured_runtime().resolve_transition(
            transition=attempt.transition,
            execution_status=WorkItemExecutionStatus.UNKNOWN,
            transition_status=WorkItemTransitionStatus.UNKNOWN,
            error=f"Linear transition outcome is unknown: {exc}",
        )
    if isinstance(outcome, WorkItemExecution):
        return outcome
    return await _configured_runtime().resolve_transition(
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
    if state_id(current) == state_id(attempt.board.states[attempt.target_status]):
        return current
    expected_state_ids = {state_id(attempt.board.states[key]) for key in attempt.expected_statuses}
    if state_id(current) not in expected_state_ids:
        return await _configured_runtime().resolve_transition(
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
        team_key=client.team_key,
    )
    issue = await client.get_issue(issue_id)
    if issue is None:
        raise LinearWorkspaceIssueError("Linear issue does not exist")
    project = issue.get("project")
    if not isinstance(project, dict) or project.get("id") != board.project.get("id"):
        raise LinearWorkspaceIssueError(_WORKSPACE_ISSUE_REQUIRED)
    return issue, board


async def _pending_transition(execution_id: str, request_id: str) -> WorkItemTransition:
    transition = await _configured_runtime().get_transition_by_request(request_id)
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
