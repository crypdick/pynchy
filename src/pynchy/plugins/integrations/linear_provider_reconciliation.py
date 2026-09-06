"""Recover managed Linear executions from missed provider callbacks."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves these annotations at runtime.
    Awaitable,
    Callable,
    Mapping,
)
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from pynchy.logger import logger
from pynchy.plugins.integrations.linear_accounts import linear_account_for_workspace
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001 - beartype resolves controller annotations.
    LinearWorkspaceBoard,
)
from pynchy.plugins.integrations.linear_statuses import (
    AWAITING_REVIEW_STATUS,
    FOLLOW_UPS_STATUS,
    TERMINAL_STATE_TYPES,
)
from pynchy.plugins.integrations.linear_work_item_completion import (
    complete_reviewed_work_item,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    reconcile_work_item,
    state_id,
)
from pynchy.plugins.integrations.linear_work_item_tasks import (
    LinearDecisionClient,
    decision_state_id,
)
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransition,
    WorkItemTransitionStatus,
)

if TYPE_CHECKING:
    from pynchy.plugins.integrations.linear_client import LinearClient

# A transition can spend one 30-second client timeout reading and another writing.
_PENDING_TRANSITION_GRACE = timedelta(minutes=2)
_RECONCILABLE_EXECUTION_STATUSES = frozenset(
    {
        WorkItemExecutionStatus.CLAIMING,
        WorkItemExecutionStatus.IN_PROGRESS,
        WorkItemExecutionStatus.AWAITING_REVIEW,
        WorkItemExecutionStatus.FOLLOW_UPS,
        WorkItemExecutionStatus.BLOCKED,
        WorkItemExecutionStatus.UNKNOWN,
    }
)
_HARD_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        WorkItemExecutionStatus.COMPLETED,
        WorkItemExecutionStatus.CANCELLED,
        WorkItemExecutionStatus.HANDED_OFF,
        WorkItemExecutionStatus.FAILED,
    }
)
_EXECUTION_PROVIDER_STATUS = {
    WorkItemExecutionStatus.CLAIMING: "in_progress",
    WorkItemExecutionStatus.IN_PROGRESS: "in_progress",
    WorkItemExecutionStatus.AWAITING_REVIEW: AWAITING_REVIEW_STATUS,
    WorkItemExecutionStatus.FOLLOW_UPS: FOLLOW_UPS_STATUS,
    WorkItemExecutionStatus.BLOCKED: "blocked",
    WorkItemExecutionStatus.UNKNOWN: "in_progress",
}
_PROVIDER_DRIFT_BLOCKER = "Linear state no longer authorizes this execution"


@dataclass(frozen=True)
class LinearDecisionInboxRuntime:
    """Durable cleanup operations used by provider-state reconciliation."""

    list_executions: Callable[..., Awaitable[list[WorkItemExecution]]]
    list_terminal_repair_candidates: Callable[[], Awaitable[list[WorkItemExecution]]]
    get_latest_unresolved_transition: Callable[[str], Awaitable[WorkItemTransition | None]]
    cancel_execution: Callable[..., Awaitable[WorkItemExecution]]
    retire_execution: Callable[[WorkItemExecution], Awaitable[None]]
    retire_terminal_execution_if_unowned: Callable[[WorkItemExecution], Awaitable[bool]]
    retire_terminal_execution: Callable[[WorkItemExecution, str | None], Awaitable[None]]


@dataclass(frozen=True)
class _ExecutionController:
    workspace: str
    board: LinearWorkspaceBoard
    issue: dict[str, Any] | None


@dataclass
class UnavailableExecutionProbe:
    """Configured accounts that independently could not load one historical issue."""

    execution: WorkItemExecution
    account_names: set[str]


@dataclass(frozen=True)
class _AccountScope:
    """Ownership evidence accumulated during one configured account pass."""

    name: str | None
    owns_execution: bool
    unavailable_probes: dict[str, UnavailableExecutionProbe] | None


_runtime: LinearDecisionInboxRuntime | None = None


def configure_linear_decision_inbox_runtime(runtime: LinearDecisionInboxRuntime) -> None:
    """Set durable cleanup operations for missed Linear callbacks."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> LinearDecisionInboxRuntime:
    if _runtime is None:
        raise RuntimeError("Linear decision inbox runtime has not been configured")
    return _runtime


async def reconcile_provider_work_item_state(
    client: LinearDecisionClient,
    boards: Mapping[str, LinearWorkspaceBoard],
    *,
    account_name: str | None = None,
    unavailable_probes: dict[str, UnavailableExecutionProbe] | None = None,
) -> int:
    """Retire local work whose provider state changed while callbacks were offline."""
    runtime = _configured_runtime()
    project_boards = {
        project_id: (workspace, board)
        for workspace, board in boards.items()
        if isinstance(project_id := board.project.get("id"), str)
    }
    latest_by_issue: dict[str, WorkItemExecution] = {}
    for execution in await runtime.list_executions(limit=None):
        current = latest_by_issue.get(execution.linear_issue_id)
        if current is None or execution.attempt > current.attempt:
            latest_by_issue[execution.linear_issue_id] = execution
    terminal_repair_ids: set[str] = set()
    retired = 0
    for execution in await runtime.list_terminal_repair_candidates():
        latest = latest_by_issue.get(execution.linear_issue_id)
        if latest is None:
            continue
        if latest.id == execution.id:
            terminal_repair_ids.add(execution.id)
            continue
        try:
            retired += await runtime.retire_terminal_execution_if_unowned(execution)
        except Exception:  # noqa: BLE001 - one stale runtime must not strand the account.
            logger.exception(
                "Superseded Linear execution retirement failed",
                issue=execution.linear_issue_identifier,
                workspace=execution.workspace,
            )

    for execution in latest_by_issue.values():
        if (
            execution.status not in _RECONCILABLE_EXECUTION_STATUSES
            and execution.id not in terminal_repair_ids
        ):
            continue
        account = linear_account_for_workspace(execution.workspace)
        if account_name is not None and account is not None and account.name != account_name:
            continue
        account_owns_execution = account_name is None or (
            account is not None and account.name == account_name
        )
        try:
            retired += await _reconcile_execution_for_account(
                client,
                execution,
                project_boards,
                _AccountScope(
                    name=account_name,
                    owns_execution=account_owns_execution,
                    unavailable_probes=unavailable_probes,
                ),
            )
        except Exception:  # noqa: BLE001 - one provider item must not strand the account.
            logger.exception(
                "Linear provider-state reconciliation failed",
                issue=execution.linear_issue_identifier,
                workspace=execution.workspace,
            )
    return retired


async def retire_globally_unavailable_work_item(execution: WorkItemExecution) -> bool:
    """Fail closed after every configured account proves an issue unavailable."""
    if execution.status in _HARD_TERMINAL_EXECUTION_STATUSES:
        await _configured_runtime().retire_terminal_execution_if_unowned(execution)
        return True
    return await _cancel_deauthorized_execution(
        execution,
        None,
        reason="issue is unavailable",
    )


async def _reconcile_execution_for_account(
    client: LinearDecisionClient,
    execution: WorkItemExecution,
    project_boards: Mapping[str, tuple[str, LinearWorkspaceBoard]],
    account_scope: _AccountScope,
) -> bool:
    issue = await client.get_issue(execution.linear_issue_id)
    if issue is None and not account_scope.owns_execution:
        if account_scope.name is not None and account_scope.unavailable_probes is not None:
            probe = account_scope.unavailable_probes.setdefault(
                execution.id,
                UnavailableExecutionProbe(execution, set()),
            )
            probe.account_names.add(account_scope.name)
        return False
    controller = _controller_for_issue(issue, project_boards)
    if controller is None:
        if execution.status in _HARD_TERMINAL_EXECUTION_STATUSES:
            await _configured_runtime().retire_terminal_execution_if_unowned(execution)
            return False
        return await _cancel_deauthorized_execution(
            execution,
            issue,
            reason="issue is unavailable" if issue is None else "issue left managed projects",
        )
    return await _reconcile_provider_execution(client, execution, controller)


def _controller_for_issue(
    issue: dict[str, Any] | None,
    project_boards: Mapping[str, tuple[str, LinearWorkspaceBoard]],
) -> _ExecutionController | None:
    project = issue.get("project") if issue is not None else None
    project_id = project.get("id") if isinstance(project, dict) else None
    controller = project_boards.get(project_id) if isinstance(project_id, str) else None
    return _ExecutionController(*controller, issue) if controller is not None else None


async def _reconcile_provider_execution(
    client: LinearDecisionClient,
    execution: WorkItemExecution,
    controller: _ExecutionController,
) -> bool:
    if execution.status in _HARD_TERMINAL_EXECUTION_STATUSES:
        return await _reconcile_terminal_candidate(execution, controller)
    runtime = _configured_runtime()
    transition = await runtime.get_latest_unresolved_transition(execution.id)
    if transition is not None:
        if _pending_transition_is_fresh(transition):
            return False
        resolved = await reconcile_work_item(
            cast("LinearClient", client),
            controller.workspace,
            execution.linear_issue_id,
            transition,
        )
        if resolved is None:
            return False
        if resolved.status not in _RECONCILABLE_EXECUTION_STATUSES:
            await _retire_execution(resolved, controller.issue)
            return True
        execution = resolved
    elif execution.status is WorkItemExecutionStatus.UNKNOWN:
        logger.warning(
            "Uncertain Linear execution lacks a transition to reconcile",
            issue=execution.linear_issue_identifier,
            execution_id=execution.id,
        )
        return False
    return await _reconcile_known_execution(execution, controller)


async def _reconcile_terminal_candidate(
    execution: WorkItemExecution,
    controller: _ExecutionController,
) -> bool:
    """Repair stale projections only when current provider state closes the route."""
    issue = controller.issue
    if issue is None:
        raise AssertionError("Known Linear execution lost its provider issue")
    current_state = state_id(issue)
    if current_state == decision_state_id(controller.board, "done") or _provider_issue_is_terminal(
        issue
    ):
        await _configured_runtime().retire_terminal_execution(
            execution,
            _provider_revision(issue),
        )
        return True
    # The local execution is terminal, so its exact task and turn are stale.
    # Provider nonterminal state still owns the conversation and session: inbox
    # admission may create a newer execution for that reopened issue.
    await _configured_runtime().retire_terminal_execution_if_unowned(execution)
    return False


def _pending_transition_is_fresh(transition: WorkItemTransition) -> bool:
    """Keep the poller behind the provider call that owns a pending intent."""
    if transition.status is not WorkItemTransitionStatus.PENDING:
        return False
    try:
        created_at = datetime.fromisoformat(transition.created_at)
    except ValueError:
        logger.warning(
            "Pending Linear transition has an invalid timestamp",
            transition_id=transition.id,
            created_at=transition.created_at,
        )
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - created_at.astimezone(UTC) < _PENDING_TRANSITION_GRACE


async def _reconcile_known_execution(
    execution: WorkItemExecution,
    controller: _ExecutionController,
) -> bool:
    """Reconcile one execution whose local provider transition is settled."""
    issue = controller.issue
    if issue is None:
        raise AssertionError("Known Linear execution lost its provider issue")
    runtime = _configured_runtime()
    current_state = state_id(issue)
    expected_status = _EXECUTION_PROVIDER_STATUS[execution.status]
    if current_state == decision_state_id(controller.board, expected_status):
        return False
    if current_state == decision_state_id(controller.board, "done"):
        updated_at = issue.get("updatedAt")
        delivery_id = (
            f"reconcile:{execution.id}:"
            f"{updated_at if isinstance(updated_at, str) else current_state}"
        )
        completed = await complete_reviewed_work_item(
            execution.workspace,
            execution.linear_issue_id,
            delivery_id,
            controller_workspace=controller.workspace,
        )
        if completed is None:
            logger.warning(
                "Linear Done reconciliation could not settle execution",
                issue=execution.linear_issue_identifier,
                execution_id=execution.id,
            )
            return False
        await runtime.retire_terminal_execution(completed, _provider_revision(issue))
        return True
    authorized_states = {
        decision_state_id(controller.board, status)
        for status in set(_EXECUTION_PROVIDER_STATUS.values())
    }
    if current_state in authorized_states:
        return False
    state = issue.get("state")
    state_name = state.get("name") if isinstance(state, dict) else current_state
    return await _cancel_deauthorized_execution(
        execution,
        issue,
        reason=str(state_name),
        terminal_authority=_provider_issue_is_terminal(issue),
    )


async def _cancel_deauthorized_execution(
    execution: WorkItemExecution,
    issue: dict[str, Any] | None,
    *,
    reason: str,
    terminal_authority: bool = False,
) -> bool:
    runtime = _configured_runtime()
    cancelled = await runtime.cancel_execution(
        execution.id,
        blocker=f"{_PROVIDER_DRIFT_BLOCKER}: {reason}",
    )
    if cancelled.status is not WorkItemExecutionStatus.CANCELLED:
        return False
    await _retire_execution(
        cancelled,
        issue if terminal_authority else None,
    )
    return True


async def _retire_execution(
    execution: WorkItemExecution,
    issue: dict[str, Any] | None,
) -> None:
    runtime = _configured_runtime()
    if issue is not None and _provider_issue_is_terminal(issue):
        await runtime.retire_terminal_execution(execution, _provider_revision(issue))
        return
    await runtime.retire_execution(execution)


def _provider_issue_is_terminal(issue: dict[str, Any]) -> bool:
    state = issue.get("state")
    state_type = state.get("type") if isinstance(state, dict) else None
    return state_type in TERMINAL_STATE_TYPES


def _provider_revision(issue: dict[str, Any] | None) -> str | None:
    revision = issue.get("updatedAt") if issue is not None else None
    return revision if isinstance(revision, str) and revision else None
