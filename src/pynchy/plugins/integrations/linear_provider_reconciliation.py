"""Recover managed Linear executions from missed provider callbacks."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves these annotations at runtime.
    Awaitable,
    Callable,
    Mapping,
)
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pynchy.logger import logger
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001 - beartype resolves controller annotations.
    LinearWorkspaceBoard,
)
from pynchy.plugins.integrations.linear_statuses import (
    AWAITING_REVIEW_STATUS,
    FOLLOW_UPS_STATUS,
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
from pynchy.work_items.api import WorkItemExecution, WorkItemExecutionStatus

if TYPE_CHECKING:
    from pynchy.plugins.integrations.linear_client import LinearClient

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
    get_latest_unresolved_transition: Callable[[str], Awaitable[Any]]
    cancel_execution: Callable[..., Awaitable[WorkItemExecution]]
    retire_execution: Callable[[WorkItemExecution], Awaitable[None]]


@dataclass(frozen=True)
class _ExecutionController:
    workspace: str
    board: LinearWorkspaceBoard
    issue: dict[str, Any] | None


@dataclass
class _RuntimeState:
    runtime: LinearDecisionInboxRuntime | None = None


_runtime = _RuntimeState()


def configure_linear_decision_inbox_runtime(runtime: LinearDecisionInboxRuntime) -> None:
    """Set durable cleanup operations for missed Linear callbacks."""
    _runtime.runtime = runtime


def _configured_runtime() -> LinearDecisionInboxRuntime:
    if _runtime.runtime is None:
        raise RuntimeError("Linear decision inbox runtime has not been configured")
    return _runtime.runtime


async def reconcile_provider_work_item_state(
    client: LinearDecisionClient,
    boards: Mapping[str, LinearWorkspaceBoard],
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

    retired = 0
    for execution in latest_by_issue.values():
        if execution.status not in _RECONCILABLE_EXECUTION_STATUSES:
            continue
        try:
            retired += await _reconcile_execution_for_account(
                client,
                execution,
                boards,
                project_boards,
            )
        except Exception:  # noqa: BLE001 - one provider item must not strand the account.
            logger.exception(
                "Linear provider-state reconciliation failed",
                issue=execution.linear_issue_identifier,
                workspace=execution.workspace,
            )
    return retired


async def _reconcile_execution_for_account(
    client: LinearDecisionClient,
    execution: WorkItemExecution,
    boards: Mapping[str, LinearWorkspaceBoard],
    project_boards: Mapping[str, tuple[str, LinearWorkspaceBoard]],
) -> bool:
    issue = await client.get_issue(execution.linear_issue_id)
    controller = _controller_for_issue(issue, project_boards)
    if controller is None:
        board = boards.get(execution.workspace)
        if board is None:
            return False
        controller = _ExecutionController(execution.workspace, board, issue)
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
    runtime = _configured_runtime()
    if controller.issue is None:
        cancelled = await runtime.cancel_execution(
            execution.id,
            blocker=f"{_PROVIDER_DRIFT_BLOCKER}: issue is unavailable",
        )
        await runtime.retire_execution(cancelled)
        return True
    if execution.status is WorkItemExecutionStatus.UNKNOWN:
        return await _reconcile_uncertain_execution(
            client,
            controller.workspace,
            execution,
        )
    return await _reconcile_known_execution(execution, controller)


async def _reconcile_uncertain_execution(
    client: LinearDecisionClient,
    controller_workspace: str,
    execution: WorkItemExecution,
) -> bool:
    """Resolve the exact uncertain provider write before generic drift handling."""
    runtime = _configured_runtime()
    transition = await runtime.get_latest_unresolved_transition(execution.id)
    if transition is None:
        logger.warning(
            "Uncertain Linear execution lacks a transition to reconcile",
            issue=execution.linear_issue_identifier,
            execution_id=execution.id,
        )
        return False
    resolved = await reconcile_work_item(
        cast("LinearClient", client),
        controller_workspace,
        execution.linear_issue_id,
        transition,
    )
    should_retire = resolved is not None and resolved.status in {
        WorkItemExecutionStatus.COMPLETED,
        WorkItemExecutionStatus.CANCELLED,
        WorkItemExecutionStatus.HANDED_OFF,
        WorkItemExecutionStatus.FAILED,
    }
    if should_retire and resolved is not None:
        await runtime.retire_execution(resolved)
    return should_retire


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
        await runtime.retire_execution(completed)
        return True
    authorized_states = {
        decision_state_id(controller.board, status)
        for status in set(_EXECUTION_PROVIDER_STATUS.values())
    }
    if current_state in authorized_states:
        return False
    state = issue.get("state")
    state_name = state.get("name") if isinstance(state, dict) else current_state
    cancelled = await runtime.cancel_execution(
        execution.id,
        blocker=f"{_PROVIDER_DRIFT_BLOCKER}: {state_name}",
    )
    await runtime.retire_execution(cancelled)
    return True
