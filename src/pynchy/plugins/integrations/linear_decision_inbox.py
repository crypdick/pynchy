"""Discover actionable decisions for the managed Linear work-item controller."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves these annotations at runtime.
    Awaitable,
    Callable,
    Iterable,
    Mapping,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pynchy.linear_plan_types import (  # noqa: TC001 - beartype resolves this annotation at runtime.
    LinearPlanReviewAdmission,
)
from pynchy.logger import logger
from pynchy.plugins.integrations.linear_accounts import linear_account_for_workspace
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001 - beartype resolves inbox annotations at runtime.
    LinearWorkspaceBoard,
    WorkspaceLike,
)
from pynchy.plugins.integrations.linear_plan_admission import (  # noqa: TC001 - beartype resolves this annotation at runtime.
    LinearPlanReviewer,
)
from pynchy.plugins.integrations.linear_planning_tasks import admit_planning_issue
from pynchy.plugins.integrations.linear_statuses import (
    AWAITING_REVIEW_STATUS,
    FOLLOW_UPS_STATUS,
    HUMAN_APPROVED_STATUS,
    READY_FOR_PLANNING_STATUS,
)
from pynchy.plugins.integrations.linear_work_item_completion import (
    complete_reviewed_work_item,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    linear_client,
    state_id,
    workspace_issue,
)
from pynchy.plugins.integrations.linear_work_item_tasks import (
    DecisionAdmission,
    DecisionIssue,
    LinearDecisionClient,
    LinearPlanReviewDeferrer,
    admit_decision_issue,
    decision_state_id,
)
from pynchy.scheduling.api import (
    ScheduledTask,  # noqa: TC001 - beartype resolves controller annotations.
)
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
)

# NOTE: Keep docs/integrations/linear.md "Receive Linear callbacks" in sync.
_PAGE_SIZE = 50
_DECISION_STATUSES = (
    READY_FOR_PLANNING_STATUS,
    HUMAN_APPROVED_STATUS,
    "in_progress",
    FOLLOW_UPS_STATUS,
)
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
    cancel_execution: Callable[..., Awaitable[WorkItemExecution]]
    retire_execution: Callable[[WorkItemExecution], Awaitable[None]]


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


async def _list_state_issues(
    client: LinearDecisionClient,
    state_id: str,
) -> list[DecisionIssue]:
    issues: list[DecisionIssue] = []
    after: str | None = None
    while True:
        data = await client.query(
            """
            query PynchyLinearDecisionInbox(
              $state_id: String!,
              $first: Int!,
              $after: String
            ) {
              workflowState(id: $state_id) {
                issues(first: $first, after: $after, orderBy: updatedAt) {
                  nodes {
                    id identifier title description url updatedAt
                    state { id }
                    project { id name }
                  }
                  pageInfo { hasNextPage endCursor }
                }
              }
            }
            """,
            state_id=state_id,
            first=_PAGE_SIZE,
            after=after,
        )
        workflow_state = data.get("workflowState")
        if workflow_state is None:
            raise ValueError("Linear decision workflow state was not found")
        if not isinstance(workflow_state, dict):
            raise TypeError("Linear decision workflow state was not an object")
        connection = workflow_state.get("issues")
        if not isinstance(connection, dict):
            raise TypeError("Linear decision workflow state issues were not an object")
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise TypeError("Linear decision issue nodes were not an array")
        issues.extend(
            issue for node in nodes if (issue := DecisionIssue.from_payload(node)) is not None
        )
        after = _next_cursor(connection)
        if after is None:
            return issues


def _next_cursor(connection: dict[str, Any]) -> str | None:
    page_info = connection.get("pageInfo")
    if not isinstance(page_info, dict):
        raise TypeError("Linear decision issue pageInfo was not an object")
    has_next = page_info.get("hasNextPage")
    if not isinstance(has_next, bool):
        raise TypeError("Linear decision issue pagination flag was not boolean")
    if not has_next:
        return None
    cursor = page_info.get("endCursor")
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("Linear decision issue pagination cursor was invalid")
    return cursor


def _project_workspaces(
    workspaces: Iterable[WorkspaceLike],
    boards: Mapping[str, LinearWorkspaceBoard],
) -> dict[str, WorkspaceLike]:
    by_folder = {workspace.folder: workspace for workspace in workspaces}
    result: dict[str, WorkspaceLike] = {}
    for folder, board in boards.items():
        project_id = board.project.get("id")
        workspace = by_folder.get(folder)
        if isinstance(project_id, str) and workspace is not None:
            result[project_id] = workspace
    return result


async def reconcile_linear_decision_inbox(  # noqa: PLR0913 - explicit controller dependencies.
    client: LinearDecisionClient,
    workspaces: Iterable[WorkspaceLike],
    boards: Mapping[str, LinearWorkspaceBoard],
    *,
    now: datetime | None = None,
    public_source: bool = True,
    review_plan: LinearPlanReviewer | None = None,
    broadcast_host_message: Callable[[str, str], Awaitable[None]] | None = None,
    defer_plan_review: LinearPlanReviewDeferrer | None = None,
) -> list[ScheduledTask]:
    """Admit one issue-conversation task for every actionable decision."""
    project_workspaces = _project_workspaces(workspaces, boards)
    created: list[ScheduledTask] = []
    observed_at = now or datetime.now(UTC)
    admission = DecisionAdmission(
        client,
        observed_at,
        public_source,
        review_plan,
        broadcast_host_message,
        defer_plan_review,
    )
    sample_board = next(iter(boards.values()), None)
    if sample_board is None:
        return created
    for status in _DECISION_STATUSES:
        state_id = decision_state_id(sample_board, status)
        for issue in await _list_state_issues(client, state_id):
            workspace = project_workspaces.get(issue.project_id)
            if workspace is None or issue.state_id != state_id:
                continue
            try:
                if status == READY_FOR_PLANNING_STATUS:
                    admitted = await admit_planning_issue(
                        issue,
                        workspace,
                        observed_at=observed_at,
                        public_source=public_source,
                    )
                else:
                    admitted = await admit_decision_issue(
                        issue,
                        workspace,
                        boards[workspace.folder],
                        status,
                        admission,
                    )
            except Exception:  # noqa: BLE001 - one malformed issue must not strand others.
                logger.exception(
                    "Linear work item admission failed",
                    issue=issue.identifier,
                    workspace=workspace.folder,
                    status=status,
                )
                continue
            if admitted is not None:
                created.append(admitted)
    return created


async def reconcile_provider_work_item_state(
    client: LinearDecisionClient,
    boards: Mapping[str, LinearWorkspaceBoard],
) -> int:
    """Retire local work whose provider state changed while callbacks were offline."""
    runtime = _configured_runtime()
    retired = 0
    for workspace, board in boards.items():
        executions = await runtime.list_executions(workspace=workspace)
        latest_by_issue: dict[str, WorkItemExecution] = {}
        for execution in executions:
            current = latest_by_issue.get(execution.linear_issue_id)
            if current is None or execution.attempt > current.attempt:
                latest_by_issue[execution.linear_issue_id] = execution
        for execution in latest_by_issue.values():
            if execution.status not in _RECONCILABLE_EXECUTION_STATUSES:
                continue
            try:
                retired += await _reconcile_provider_execution(
                    client,
                    workspace,
                    board,
                    execution,
                )
            except Exception:  # noqa: BLE001 - one provider item must not strand the account.
                logger.exception(
                    "Linear provider-state reconciliation failed",
                    issue=execution.linear_issue_identifier,
                    workspace=workspace,
                )
    return retired


async def _reconcile_provider_execution(
    client: LinearDecisionClient,
    workspace: str,
    board: LinearWorkspaceBoard,
    execution: WorkItemExecution,
) -> bool:
    runtime = _configured_runtime()
    issue = await client.get_issue(execution.linear_issue_id)
    if issue is None:
        cancelled = await runtime.cancel_execution(
            execution.id,
            blocker=f"{_PROVIDER_DRIFT_BLOCKER}: issue is unavailable",
        )
        await runtime.retire_execution(cancelled)
        return True
    current_state = state_id(issue)
    expected_status = _EXECUTION_PROVIDER_STATUS[execution.status]
    if current_state == decision_state_id(board, expected_status):
        return False
    if current_state == decision_state_id(board, "done"):
        updated_at = issue.get("updatedAt")
        delivery_id = (
            f"reconcile:{execution.id}:"
            f"{updated_at if isinstance(updated_at, str) else current_state}"
        )
        completed = await complete_reviewed_work_item(
            workspace,
            execution.linear_issue_id,
            delivery_id,
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
        decision_state_id(board, status) for status in set(_EXECUTION_PROVIDER_STATUS.values())
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


async def reconcile_all_linear_work_items(
    workspaces: Mapping[str, WorkspaceLike],
    boards: Mapping[str, LinearWorkspaceBoard],
    *,
    review_plan: LinearPlanReviewer,
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
    defer_plan_review: LinearPlanReviewDeferrer,
) -> list[ScheduledTask]:
    """Run one managed-board controller pass across configured Linear accounts."""
    admitted: list[ScheduledTask] = []
    for account_name, account_boards in _boards_by_account(boards).items():
        workspace = next(iter(account_boards))
        account = linear_account_for_workspace(workspace)
        if account is None or account.config.public_source == "forbidden":
            continue
        try:
            async with linear_client(workspace=workspace) as client:
                retired = await reconcile_provider_work_item_state(client, account_boards)
                if retired:
                    logger.warning(
                        "Retired Linear work after provider-state reconciliation",
                        account=account_name,
                        count=retired,
                    )
                admitted.extend(
                    await reconcile_linear_decision_inbox(
                        client,
                        workspaces.values(),
                        account_boards,
                        public_source=account.config.public_source is not False,
                        review_plan=review_plan,
                        broadcast_host_message=broadcast_host_message,
                        defer_plan_review=defer_plan_review,
                    )
                )
        except Exception:  # noqa: BLE001 - one optional account must not stop other accounts.
            logger.exception(
                "Linear work item reconciliation failed",
                account=account_name,
                workspace=workspace,
            )
    if admitted:
        logger.info("Linear work item tasks admitted", count=len(admitted))
    return admitted


async def process_linear_plan_review_admission(
    admission: LinearPlanReviewAdmission,
    workspaces: Iterable[WorkspaceLike],
    *,
    review_plan: LinearPlanReviewer,
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
) -> ScheduledTask | None:
    """Review and lease one exact issue revision in its own provider session."""
    workspace = next(
        (candidate for candidate in workspaces if candidate.folder == admission.workspace),
        None,
    )
    if workspace is None:
        raise ValueError("Linear plan review workspace is no longer configured")
    async with linear_client(workspace=workspace.folder) as client:
        payload, board = await workspace_issue(client, workspace.folder, admission.issue_id)
        issue = DecisionIssue.from_payload(payload)
        if (
            issue is None
            or issue.identifier != admission.identifier
            or issue.updated_at != admission.updated_at
            or issue.state_id != decision_state_id(board, HUMAN_APPROVED_STATUS)
        ):
            return None
        return await admit_decision_issue(
            issue,
            workspace,
            board,
            HUMAN_APPROVED_STATUS,
            DecisionAdmission(
                client=client,
                observed_at=datetime.now(UTC),
                public_source=admission.public_source,
                review_plan=review_plan,
                broadcast_host_message=broadcast_host_message,
            ),
        )


def _boards_by_account(
    boards: Mapping[str, LinearWorkspaceBoard],
) -> dict[str, dict[str, LinearWorkspaceBoard]]:
    """Partition boards by the account selected by their workspace."""
    result: dict[str, dict[str, LinearWorkspaceBoard]] = {}
    for workspace, board in boards.items():
        account = linear_account_for_workspace(workspace)
        if account is None:
            continue
        result.setdefault(account.name, {})[workspace] = board
    return result
