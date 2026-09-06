"""Discover actionable decisions for the managed Linear work-item controller."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
    Iterable,
    Mapping,
)
from datetime import UTC, datetime
from typing import Any, cast

from pynchy.linear_plan_types import (
    LinearPlanReviewAdmission,
    LinearPlanReviewBlockedError,
    LinearPlanReviewError,
)
from pynchy.logger import logger
from pynchy.plugins.integrations.linear_accounts import (
    LinearAccount,
    configured_linear_accounts,
    linear_account_for_workspace,
)
from pynchy.plugins.integrations.linear_boards import (
    LinearWorkspaceBoard,
    WorkspaceLike,
)
from pynchy.plugins.integrations.linear_issue_mutations import update_issue_state
from pynchy.plugins.integrations.linear_plan_admission import (
    LinearPlanReviewer,
)
from pynchy.plugins.integrations.linear_planning_tasks import admit_planning_issue
from pynchy.plugins.integrations.linear_provider_reconciliation import (
    UnavailableExecutionProbe,
    reconcile_provider_work_item_state,
    retire_globally_unavailable_work_item,
)
from pynchy.plugins.integrations.linear_statuses import (
    FOLLOW_UPS_STATUS,
    HUMAN_APPROVED_STATUS,
    READY_FOR_PLANNING_STATUS,
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
    _report_plan_review_status,
    _reset_plan_review_context,
    admit_decision_issue,
    decision_state_id,
)
from pynchy.scheduling.api import (
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
    boards_by_account = _boards_by_account(boards)
    accounts = {account.name: account for account in configured_linear_accounts()}
    for account_boards in boards_by_account.values():
        workspace = next(iter(account_boards))
        account = cast("LinearAccount", linear_account_for_workspace(workspace))
        accounts.setdefault(account.name, account)
    eligible_accounts = {
        name: account
        for name, account in accounts.items()
        if account.config.public_source != "forbidden"
    }
    reconciled_accounts: set[str] = set()
    unavailable_probes: dict[str, UnavailableExecutionProbe] = {}
    for account_name, account in eligible_accounts.items():
        account_boards = boards_by_account.get(account_name, {})
        workspace = next(iter(account_boards), account_name)
        try:  # noqa: PLW0717 - one account client owns recovery and admission.
            async with linear_client(account_name=account_name) as client:
                retired = await reconcile_provider_work_item_state(
                    client,
                    account_boards,
                    account_name=account_name,
                    unavailable_probes=unavailable_probes,
                )
                reconciled_accounts.add(account_name)
                if retired:
                    logger.warning(
                        "Retired Linear work after provider-state reconciliation",
                        account=account_name,
                        count=retired,
                    )
                if account_boards:
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
    if reconciled_accounts == set(eligible_accounts):
        for probe in unavailable_probes.values():
            if probe.account_names == reconciled_accounts:
                await retire_globally_unavailable_work_item(probe.execution)
    if admitted:
        logger.info("Linear work item tasks admitted", count=len(admitted))
    return admitted


async def process_linear_plan_review_admission(  # noqa: PLR0913 - exact review admission dependencies stay explicit.
    admission: LinearPlanReviewAdmission,
    workspaces: Iterable[WorkspaceLike],
    *,
    review_plan: LinearPlanReviewer,
    broadcast_host_message: Callable[[str, str], Awaitable[None]],
    attempt: int = 1,
    reset_context: Callable[[str], Awaitable[None]] | None = None,
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
        context = DecisionAdmission(
            client=client,
            observed_at=datetime.now(UTC),
            public_source=admission.public_source,
            review_plan=review_plan,
            broadcast_host_message=broadcast_host_message,
            plan_review_attempt=attempt,
            reset_context=reset_context,
        )
        try:
            return await admit_decision_issue(
                issue,
                workspace,
                board,
                HUMAN_APPROVED_STATUS,
                context,
            )
        except LinearPlanReviewError as exc:
            if attempt < 3:
                raise
            await update_issue_state(client, issue.id, state_id(board.states["blocked"]))
            await _report_plan_review_status(
                issue,
                workspace,
                context,
                "Plan review failed after three attempts and this issue was moved to Blocked. "
                "Fix it, then move it to Human Approved to retry.",
            )
            await _reset_plan_review_context(issue, workspace, context)
            logger.error(
                "Linear plan review exhausted and issue was blocked",
                issue=issue.identifier,
                error=str(exc),
            )
            raise LinearPlanReviewBlockedError(str(exc)) from exc


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
