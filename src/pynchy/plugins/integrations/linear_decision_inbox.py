"""Discover actionable decisions for the managed Linear work-item controller."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves these annotations at runtime.
    Awaitable,
    Callable,
    Iterable,
    Mapping,
)
from datetime import UTC, datetime
from typing import Any

from pynchy.logger import logger
from pynchy.plugins.integrations.linear_accounts import linear_account_for_workspace
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001, RUF100 - beartype resolves inbox annotations at runtime.
    LinearWorkspaceBoard,
    WorkspaceLike,
)
from pynchy.plugins.integrations.linear_plan_admission import (  # noqa: TC001, RUF100 - beartype resolves this annotation at runtime.
    LinearPlanReviewer,
)
from pynchy.plugins.integrations.linear_planning_tasks import admit_planning_issue
from pynchy.plugins.integrations.linear_statuses import (
    FOLLOW_UPS_STATUS,
    HUMAN_APPROVED_STATUS,
    READY_FOR_PLANNING_STATUS,
)
from pynchy.plugins.integrations.linear_work_item_provider import linear_client
from pynchy.plugins.integrations.linear_work_item_tasks import (
    DecisionAdmission,
    DecisionIssue,
    LinearDecisionClient,
    admit_decision_issue,
    decision_state_id,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves controller annotations.
    ScheduledTask,
)

# NOTE: Keep docs/integrations/linear.md "Receive Linear callbacks" in sync.
_PAGE_SIZE = 50
_DECISION_STATUSES = (
    READY_FOR_PLANNING_STATUS,
    HUMAN_APPROVED_STATUS,
    "in_progress",
    FOLLOW_UPS_STATUS,
)


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


async def reconcile_linear_decision_inbox(  # noqa: PLR0913, RUF100 - explicit controller dependencies.
    client: LinearDecisionClient,
    workspaces: Iterable[WorkspaceLike],
    boards: Mapping[str, LinearWorkspaceBoard],
    *,
    now: datetime | None = None,
    public_source: bool = True,
    review_plan: LinearPlanReviewer | None = None,
    broadcast_host_message: Callable[[str, str], Awaitable[None]] | None = None,
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
            except Exception:  # noqa: BLE001, RUF100 - one malformed issue must not strand others.
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


async def reconcile_all_linear_work_items(
    workspaces: Mapping[str, WorkspaceLike],
    boards: Mapping[str, LinearWorkspaceBoard],
    *,
    review_plan: LinearPlanReviewer,
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
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
                admitted.extend(
                    await reconcile_linear_decision_inbox(
                        client,
                        workspaces.values(),
                        account_boards,
                        public_source=account.config.public_source is not False,
                        review_plan=review_plan,
                        broadcast_host_message=broadcast_host_message,
                    )
                )
        except Exception:  # noqa: BLE001, RUF100 - one optional account must not stop other accounts.
            logger.exception(
                "Linear work item reconciliation failed",
                account=account_name,
                workspace=workspace,
            )
    if admitted:
        logger.info("Linear work item tasks admitted", count=len(admitted))
    return admitted


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
