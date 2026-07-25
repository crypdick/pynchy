"""Reconcile actionable Linear decisions for workspaces without webhook routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves these annotations at runtime.
    Iterable,
    Mapping,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from pynchy.config import get_settings
from pynchy.host.container_manager.security.fencing import fence_untrusted_content
from pynchy.logger import logger
from pynchy.plugins.integrations.linear_accounts import linear_account_for_workspace
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001, RUF100 - beartype resolves inbox annotations at runtime.
    LinearWorkspaceBoard,
    WorkspaceLike,
)
from pynchy.plugins.integrations.linear_statuses import (
    FOLLOW_UPS_STATUS,
    HUMAN_APPROVED_STATUS,
)
from pynchy.plugins.integrations.linear_webhooks import LinearPluginOptions
from pynchy.plugins.integrations.linear_work_item_provider import (
    WorkItemLeaseRequest,
    acquire_work_item_lease,
    linear_client,
)
from pynchy.state import (
    WorkItemClaimConflictError,
    create_task_if_absent,
    get_active_work_item_execution,
)
from pynchy.types import ScheduledTask, WorkItemExecutionStatus

if TYPE_CHECKING:
    from pynchy.plugins.integrations.linear_client import LinearClient

# NOTE: Keep docs/integrations/linear.md "Receive Linear callbacks" in sync.
LINEAR_DECISION_POLL_SECONDS = 60
_PAGE_SIZE = 50
_DECISION_STATUSES = (HUMAN_APPROVED_STATUS, "in_progress", FOLLOW_UPS_STATUS)
_EXECUTION_CONTRACT = (
    "Objective: deliver the Human Approved Linear work item below.\n"
    "Authority: the host verified approval and acquired the execution lease.\n"
    "Success: leave the issue state, comments, and attachments accurately reflecting the "
    "outcome. If the work produces pull requests, attach every PR to the issue with "
    "linear_create_attachment before requesting review. Use your judgment to move the issue "
    "through Awaiting Review, Follow-ups, and Done. Follow-ups are for final operational work "
    "such as deployment verification, preserving useful logs before teardown, cleaning feature "
    "resources, and updating or unblocking related issues."
)
_FOLLOW_UPS_CONTRACT = (
    "Objective: finish the Follow-ups for the Linear work item below.\n"
    "Authority: the underlying work was already Human Approved.\n"
    "Success: use your judgment to finish the whole job. Verify delivery or deployment, "
    "preserve useful logs before teardown, clean feature resources, and update or unblock "
    "related issues when relevant. Record useful context and move the item to Done when it is "
    "genuinely finished, or Blocked when it needs outside help."
)
_INBOX_INITIATOR = "linear-decision-inbox"


@runtime_checkable
class LinearDecisionClient(Protocol):
    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        """Run a Linear GraphQL query."""

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        """Return one issue by its durable provider ID."""


@dataclass(frozen=True)
class _DecisionIssue:
    id: str
    identifier: str
    title: str
    url: str
    updated_at: str
    state_id: str
    project_id: str

    @classmethod
    def from_payload(cls, payload: object) -> _DecisionIssue | None:
        if not isinstance(payload, dict):
            raise TypeError("Linear decision inbox issue was not an object")
        state = payload.get("state")
        project = payload.get("project")
        if not isinstance(state, dict):
            raise TypeError("Linear decision inbox issue lacks state")
        if project is None:
            return None
        if not isinstance(project, dict):
            raise TypeError("Linear decision inbox issue project was not an object")
        return cls(
            id=_text(payload, "id"),
            identifier=_text(payload, "identifier"),
            title=_text(payload, "title"),
            url=_text(payload, "url"),
            updated_at=_text(payload, "updatedAt"),
            state_id=_text(state, "id"),
            project_id=_text(project, "id"),
        )


@dataclass(frozen=True)
class _TaskAdmission:
    status: str
    public_source: bool
    task_id: str | None = None


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TypeError(f"Linear decision inbox issue {key} was not text")
    if not value:
        raise ValueError(f"Linear decision inbox issue lacks {key}")
    return value


async def _list_state_issues(
    client: LinearDecisionClient,
    state_id: str,
) -> list[_DecisionIssue]:
    issues: list[_DecisionIssue] = []
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
                    id identifier title url updatedAt
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
            issue for node in nodes if (issue := _DecisionIssue.from_payload(node)) is not None
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


def _task_for_issue(
    issue: _DecisionIssue,
    workspace: WorkspaceLike,
    now: datetime,
    admission: _TaskAdmission,
) -> ScheduledTask:
    occurred_at = now.astimezone(UTC).isoformat()
    digest = hashlib.sha256(
        f"{admission.status}:{issue.id}:{issue.updated_at}".encode()
    ).hexdigest()[:16]
    context = json.dumps(
        {
            "issue_id": issue.id,
            "identifier": issue.identifier,
            "title": issue.title,
            "url": issue.url,
        },
        sort_keys=True,
    )
    if admission.public_source:
        context = fence_untrusted_content(context, source="linear-decision-inbox")
    is_follow_up = admission.status == FOLLOW_UPS_STATUS
    task_prefix = "linear-follow-ups" if is_follow_up else "linear-execute"
    contract = _FOLLOW_UPS_CONTRACT if is_follow_up else _EXECUTION_CONTRACT
    input_kind = "follow-ups" if is_follow_up else "authorized"
    return ScheduledTask(
        id=admission.task_id or f"{task_prefix}-{issue.identifier.lower()}-{digest}",
        group_folder=workspace.folder,
        chat_jid=workspace.jid,
        prompt=f"{contract}\n\n{context}",
        schedule_type="once",
        schedule_value=occurred_at,
        context_mode="isolated",
        next_run=occurred_at,
        created_at=occurred_at,
        input_source=(
            f"{'external' if admission.public_source else 'trusted'}:linear:{input_kind}"
        ),
        derived_thread_name=f"[{issue.identifier}] {issue.title}"[:100],
    )


async def reconcile_linear_decision_inbox(
    client: LinearDecisionClient,
    workspaces: Iterable[WorkspaceLike],
    boards: Mapping[str, LinearWorkspaceBoard],
    *,
    now: datetime | None = None,
    public_source: bool = True,
) -> list[ScheduledTask]:
    """Admit one isolated task for every newly observed actionable decision."""
    project_workspaces = _project_workspaces(workspaces, boards)
    created: list[ScheduledTask] = []
    observed_at = now or datetime.now(UTC)
    sample_board = next(iter(boards.values()), None)
    if sample_board is None:
        return created
    for status in _DECISION_STATUSES:
        state = sample_board.states.get(status)
        state_id = state.get("id") if isinstance(state, dict) else None
        if state_id is None:
            raise ValueError(f"Linear board lacks decision state {status}")
        if not isinstance(state_id, str):
            raise TypeError(f"Linear board decision state {status} lacks a text ID")
        for issue in await _list_state_issues(client, state_id):
            workspace = project_workspaces.get(issue.project_id)
            if workspace is None or issue.state_id != state_id:
                continue
            existing = await get_active_work_item_execution(issue.id)
            recovery_task_id = (
                existing.task_id
                if status == "in_progress"
                and existing is not None
                and existing.workspace == workspace.folder
                and existing.initiated_by == _INBOX_INITIATOR
                else None
            )
            task = _task_for_issue(
                issue,
                workspace,
                observed_at,
                _TaskAdmission(
                    status=status,
                    public_source=public_source,
                    task_id=recovery_task_id,
                ),
            )
            if status == "in_progress" and (
                existing is None
                or existing.workspace != workspace.folder
                or existing.initiated_by != _INBOX_INITIATOR
                or existing.task_id != task.id
            ):
                continue
            if status == HUMAN_APPROVED_STATUS and existing is not None:
                continue
            if status == FOLLOW_UPS_STATUS:
                if existing is None and await create_task_if_absent(task):
                    created.append(task)
                continue
            try:
                execution = await acquire_work_item_lease(
                    cast("LinearClient", client),
                    WorkItemLeaseRequest(
                        workspace=workspace.folder,
                        issue_id=issue.id,
                        request_id=f"{task.id}:lease",
                        initiated_by=_INBOX_INITIATOR,
                        task_id=task.id,
                        board=boards[workspace.folder],
                    ),
                )
            except WorkItemClaimConflictError:
                continue
            if (
                execution.status is WorkItemExecutionStatus.IN_PROGRESS
                and await create_task_if_absent(task)
            ):
                created.append(task)
    return created


def polling_boards(
    boards: Mapping[str, LinearWorkspaceBoard],
) -> dict[str, LinearWorkspaceBoard]:
    """Keep webhook-routed workspaces on push delivery and poll the remainder."""
    settings = get_settings()
    plugin = settings.plugins.get("linear")
    options = LinearPluginOptions.model_validate(plugin.options if plugin is not None else {})
    if any(route.workspace is None for route in options.webhook_routes):
        return {}
    routed = {route.workspace for route in options.webhook_routes}
    return {folder: board for folder, board in boards.items() if folder not in routed}


async def start_linear_decision_inbox_loop(
    workspaces: Mapping[str, WorkspaceLike],
    boards: Mapping[str, LinearWorkspaceBoard],
) -> None:
    """Reconcile local-only Linear workspaces without requiring a public webhook."""
    fallback_boards = polling_boards(boards)
    if not fallback_boards:
        return
    while True:
        created_count = 0
        for account_name, account_boards in _boards_by_account(fallback_boards).items():
            workspace = next(iter(account_boards))
            account = linear_account_for_workspace(workspace)
            if account is None or account.config.public_source == "forbidden":
                continue
            try:
                async with linear_client(workspace=workspace) as client:
                    created = await reconcile_linear_decision_inbox(
                        client,
                        workspaces.values(),
                        account_boards,
                        public_source=account.config.public_source is not False,
                    )
                created_count += len(created)
            except Exception:  # noqa: BLE001, RUF100 - one optional account must not stop polling others.
                logger.exception(
                    "Linear decision inbox reconciliation failed",
                    account=account_name,
                    workspace=workspace,
                )
        if created_count:
            logger.info("Linear decision tasks admitted", count=created_count)
        await asyncio.sleep(LINEAR_DECISION_POLL_SECONDS)


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
