"""Durable task admission and orphan recovery for managed Linear work."""

from __future__ import annotations

# allow: file-length - task admission and recovery share one durable lease policy.
import hashlib
import json
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from pynchy.content_fencing import fence_untrusted_content
from pynchy.conversation.api import ConversationControlBinding, ConversationId
from pynchy.linear_plan_types import (
    LinearPlanReviewAdmission,
)
from pynchy.logger import logger
from pynchy.plugins.integrations.linear_accounts import linear_account_for_workspace
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001 - beartype resolves task annotations.
    LinearWorkspaceBoard,
    WorkspaceLike,
)
from pynchy.plugins.integrations.linear_conversation_identity import (
    resolve_linear_issue_conversation,
)
from pynchy.plugins.integrations.linear_plan_admission import (
    LinearPlanReviewer,
    review_approved_plan,
)
from pynchy.plugins.integrations.linear_plans import PLAN_START
from pynchy.plugins.integrations.linear_statuses import (
    FOLLOW_UPS_STATUS,
    HUMAN_APPROVED_STATUS,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    WorkItemLeaseRequest,
    acquire_work_item_lease,
)
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
    agent_task_workflow_id,
)
from pynchy.work_items.api import (
    WorkItemClaimConflictError,
    WorkItemExecution,
    WorkItemExecutionStatus,
)

if TYPE_CHECKING:
    from pynchy.plugins.integrations.linear_client import LinearClient

LinearPlanReviewDeferrer = Callable[[LinearPlanReviewAdmission], Awaitable[None]]

# NOTE: Keep docs/integrations/linear.md "Receive Linear callbacks" aligned with this policy.
_ORPHAN_RETRY_GRACE = timedelta(minutes=5)
_ORPHAN_RUN_LIMIT = 3
_CONTROLLER_INITIATOR = "linear-work-item-controller"


@runtime_checkable
class LinearDecisionClient(Protocol):
    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        """Run a Linear GraphQL query."""

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        """Return one issue by its durable provider ID."""

    async def create_comment(self, issue_id: str, body: str) -> dict[str, Any]:
        """Add one durable issue comment."""


@dataclass(frozen=True)
class DecisionIssue:
    id: str
    identifier: str
    title: str
    url: str
    description: str
    updated_at: str
    state_id: str
    project_id: str

    @classmethod
    def from_payload(cls, payload: object) -> DecisionIssue | None:
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
            description=str(payload.get("description") or ""),
            updated_at=_text(payload, "updatedAt"),
            state_id=_text(state, "id"),
            project_id=_text(project, "id"),
        )


@dataclass(frozen=True)
class _TaskAdmission:
    status: str
    public_source: bool
    task_id: str | None = None


@dataclass(frozen=True)
class DecisionAdmission:
    client: LinearDecisionClient
    observed_at: datetime
    public_source: bool
    review_plan: LinearPlanReviewer | None = None
    broadcast_host_message: Callable[[str, str], Awaitable[None]] | None = None
    defer_plan_review: LinearPlanReviewDeferrer | None = None
    plan_review_attempt: int | None = None
    reset_context: Callable[[str], Awaitable[None]] | None = None


@dataclass(frozen=True)
class LinearWorkItemTaskRuntime:
    """Durable task and execution operations selected during plugin composition."""

    get_control_binding: Callable[[ConversationId], Awaitable[ConversationControlBinding | None]]
    get_task: Callable[[str], Awaitable[ScheduledTask | None]]
    create_task: Callable[[ScheduledTask], Awaitable[bool]]
    update_task: Callable[[str, dict[str, object]], Awaitable[None]]
    get_task_logs: Callable[..., Awaitable[list[Any]]]
    bind_execution_to_task: Callable[..., Awaitable[object]]
    get_active_execution: Callable[[str], Awaitable[WorkItemExecution | None]]
    resume_once_task: Callable[[str], Awaitable[bool]]
    get_execution_for_issue: Callable[..., Awaitable[WorkItemExecution | None]]


_runtime: LinearWorkItemTaskRuntime | None = None


def configure_linear_work_item_task_runtime(runtime: LinearWorkItemTaskRuntime) -> None:
    """Set the durable operations used for Linear task admission and recovery."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> LinearWorkItemTaskRuntime:
    if _runtime is None:
        raise RuntimeError("Linear work-item task runtime has not been configured")
    return _runtime


async def get_conversation_control_binding(
    conversation_id: ConversationId,
) -> ConversationControlBinding | None:
    """Read one control binding through the configured durable capability."""
    return await _configured_runtime().get_control_binding(conversation_id)


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TypeError(f"Linear decision inbox issue {key} was not text")
    if not value:
        raise ValueError(f"Linear decision inbox issue lacks {key}")
    return value


async def linear_issue_conversation_id(
    issue_id: str,
    workspace: str,
) -> str | None:
    """Return or create the issue's sole routed runtime identity."""
    account = linear_account_for_workspace(workspace)
    if account is None:
        return None
    conversation = await resolve_linear_issue_conversation(issue_id, workspace, account.name)
    return str(conversation.id)


async def _report_plan_review_status(
    issue: DecisionIssue,
    workspace: WorkspaceLike,
    context: DecisionAdmission,
    text: str,
) -> None:
    """Post actual plan-review state to the issue's existing control thread."""
    if context.broadcast_host_message is None:
        return
    try:
        conversation_id = await linear_issue_conversation_id(issue.id, workspace.folder)
        if conversation_id is None:
            return
        binding = await get_conversation_control_binding(ConversationId(conversation_id))
        if binding is None or binding.closed:
            return
        await context.broadcast_host_message(str(binding.thread_jid), text)
    except Exception:  # noqa: BLE001 - status delivery must not change admission.
        logger.exception(
            "Linear plan review status delivery failed",
            issue=issue.identifier,
            workspace=workspace.folder,
        )


def _plan_review_attempt_status(attempt: int) -> str:
    if attempt == 1:
        return "🔎 Checking approved plan (1/3)."
    if attempt == 2:
        return "🔄 Retrying approved plan review (2/3)."
    return "🔄 Final approved plan review attempt (3/3)."


async def _reset_plan_review_context(
    issue: DecisionIssue,
    workspace: WorkspaceLike,
    context: DecisionAdmission,
) -> None:
    if context.reset_context is None:
        return
    conversation_id = await linear_issue_conversation_id(issue.id, workspace.folder)
    if conversation_id is None:
        return
    binding = await get_conversation_control_binding(ConversationId(conversation_id))
    if binding is None or binding.closed:
        return
    try:
        await context.reset_context(str(binding.thread_jid))
    except Exception:  # noqa: BLE001 - cleanup must not turn a settled review into a retry.
        logger.exception(
            "Linear plan review context reset failed",
            issue=issue.identifier,
            workspace=workspace.folder,
        )


async def _task_for_issue(
    issue: DecisionIssue,
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
    input_kind = "follow-ups" if is_follow_up else "authorized"
    return ScheduledTask(
        id=admission.task_id or f"{task_prefix}-{issue.identifier.lower()}-{digest}",
        group_folder=workspace.folder,
        chat_jid=workspace.jid,
        prompt=context,
        schedule_type="once",
        schedule_value=occurred_at,
        session_policy=SessionPolicy.CONTINUE,
        next_run=occurred_at,
        created_at=occurred_at,
        input_source=(
            f"{'external' if admission.public_source else 'trusted'}:linear:{input_kind}"
        ),
        derived_thread_name=f"[{issue.identifier}] {issue.title}"[:100],
        conversation_id=await linear_issue_conversation_id(issue.id, workspace.folder),
    )


def _execution_task_id(issue: DecisionIssue, execution: WorkItemExecution) -> str:
    if execution.task_id is not None:
        return execution.task_id
    return f"linear-execute-{issue.identifier.lower()}-{execution.id[:16]}"


def _last_run_is_recent(task: ScheduledTask, observed_at: datetime) -> bool:
    if task.last_run is None:
        return False
    try:
        last_run = datetime.fromisoformat(task.last_run)
    except ValueError:
        logger.warning(
            "Linear work item task has an invalid last-run timestamp",
            task_id=task.id,
            last_run=task.last_run,
        )
        return True
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=UTC)
    return observed_at.astimezone(UTC) - last_run.astimezone(UTC) < _ORPHAN_RETRY_GRACE


async def ensure_task_active(
    task: ScheduledTask,
    *,
    observed_at: datetime,
) -> tuple[ScheduledTask, bool]:
    """Create missing work or reactivate a quiet orphan after a grace period."""
    runtime = _configured_runtime()
    existing = await runtime.get_task(task.id)
    if existing is None:
        return task, await runtime.create_task(task)
    ownership_updates: dict[str, object] = {}
    if existing.session_policy is not SessionPolicy.CONTINUE:
        ownership_updates["session_policy"] = SessionPolicy.CONTINUE
    if task.conversation_id is not None and existing.conversation_id != task.conversation_id:
        ownership_updates["conversation_id"] = task.conversation_id
    if existing.derived_thread_name != task.derived_thread_name:
        ownership_updates["derived_thread_name"] = task.derived_thread_name
    if ownership_updates:
        await runtime.update_task(task.id, ownership_updates)
        existing = replace(
            existing,
            session_policy=SessionPolicy.CONTINUE,
            conversation_id=task.conversation_id or existing.conversation_id,
            derived_thread_name=task.derived_thread_name,
        )
    if existing.status == "active" or existing.status in {"paused", "cancelled"}:
        return existing, False
    if _last_run_is_recent(existing, observed_at):
        return existing, False

    counted_runs = sum(
        log.status in {"success", "incomplete"}
        for log in await runtime.get_task_logs(task.id, limit=10)
    )
    if counted_runs >= _ORPHAN_RUN_LIMIT:
        logger.warning(
            "Linear work item remains active after repeated completed agent runs",
            task_id=task.id,
            counted_runs=counted_runs,
        )
        return existing, False

    due_at = observed_at.astimezone(UTC).isoformat()
    # Recovery retains the task's durable runtime binding across project moves.
    resumed = replace(
        task,
        group_folder=existing.group_folder,
        chat_jid=existing.chat_jid,
        conversation_id=existing.conversation_id,
        schedule_value=due_at,
        next_run=due_at,
        last_run=existing.last_run,
        last_result=existing.last_result,
        created_at=existing.created_at,
    )
    await runtime.update_task(
        task.id,
        {
            "prompt": resumed.prompt,
            "schedule_value": resumed.schedule_value,
            "status": "active",
            "input_source": resumed.input_source,
            "derived_thread_name": resumed.derived_thread_name,
        },
    )
    logger.warning(
        "Reactivated orphaned Linear work item task",
        task_id=task.id,
        counted_runs=counted_runs,
    )
    return resumed, True


async def resume_quiet_paused_task(
    task: ScheduledTask,
    *,
    observed_at: datetime,
) -> tuple[ScheduledTask, bool]:
    """Resume a paused task only through its atomic unclaimed-turn guard."""
    if task.status != "paused" or _last_run_is_recent(task, observed_at):
        return task, False
    runtime = _configured_runtime()
    if not await runtime.resume_once_task(task.id):
        return task, False
    refreshed = await runtime.get_task(task.id)
    if refreshed is None or refreshed.status != "active":
        return task, False
    logger.warning("Resumed quiet paused Linear work item task", task_id=task.id)
    return refreshed, True


async def _bind_execution_task(
    execution: WorkItemExecution,
    task: ScheduledTask,
) -> None:
    await _configured_runtime().bind_execution_to_task(
        execution.id,
        task_id=task.id,
        temporal_workflow_id=agent_task_workflow_id(task),
    )


def decision_state_id(board: LinearWorkspaceBoard, status: str) -> str:
    """Return one required managed decision-state ID."""
    state = board.states.get(status)
    state_id = state.get("id") if isinstance(state, dict) else None
    if state_id is None:
        raise ValueError(f"Linear board lacks decision state {status}")
    if not isinstance(state_id, str):
        raise TypeError(f"Linear board decision state {status} lacks a text ID")
    return state_id


async def _admit_in_progress_issue(
    issue: DecisionIssue,
    workspace: WorkspaceLike,
    context: DecisionAdmission,
) -> ScheduledTask | None:
    runtime = _configured_runtime()
    execution = await runtime.get_active_execution(issue.id)
    if execution is None:
        logger.warning(
            "Managed Linear issue is In Progress without an execution lease",
            issue=issue.identifier,
            workspace=workspace.folder,
        )
        return None
    if execution.status is not WorkItemExecutionStatus.IN_PROGRESS:
        logger.warning(
            "Managed Linear issue has an unusable execution lease",
            issue=issue.identifier,
            workspace=workspace.folder,
            execution_id=execution.id,
            execution_status=execution.status.value,
        )
        return None
    task = await _task_for_issue(
        issue,
        workspace,
        context.observed_at,
        _TaskAdmission(
            status="in_progress",
            public_source=context.public_source,
            task_id=_execution_task_id(issue, execution),
        ),
    )
    active_task, admitted = await ensure_task_active(task, observed_at=context.observed_at)
    if active_task.status == "paused" and execution.task_id == active_task.id:
        active_task, resumed = await resume_quiet_paused_task(
            active_task,
            observed_at=context.observed_at,
        )
        admitted = admitted or resumed
    await _bind_execution_task(execution, active_task)
    return active_task if admitted else None


async def _admit_follow_ups_issue(
    issue: DecisionIssue,
    workspace: WorkspaceLike,
    context: DecisionAdmission,
) -> ScheduledTask | None:
    latest = await _configured_runtime().get_execution_for_issue(
        issue.id,
        workspace=workspace.folder,
    )
    if latest is not None and latest.status is WorkItemExecutionStatus.UNKNOWN:
        logger.warning(
            "Linear Follow-ups deferred for an uncertain execution",
            issue=issue.identifier,
            execution_id=latest.id,
        )
        return None
    task = await _task_for_issue(
        issue,
        workspace,
        context.observed_at,
        _TaskAdmission(status=FOLLOW_UPS_STATUS, public_source=context.public_source),
    )
    active_task, admitted = await ensure_task_active(task, observed_at=context.observed_at)
    return active_task if admitted else None


async def _admit_human_approved_issue(
    issue: DecisionIssue,
    workspace: WorkspaceLike,
    board: LinearWorkspaceBoard,
    context: DecisionAdmission,
) -> ScheduledTask | None:
    if await _configured_runtime().get_active_execution(issue.id) is not None:
        return None
    if PLAN_START in issue.description:
        if context.defer_plan_review is not None:
            await context.defer_plan_review(
                LinearPlanReviewAdmission(
                    workspace=workspace.folder,
                    issue_id=issue.id,
                    identifier=issue.identifier,
                    updated_at=issue.updated_at,
                    public_source=context.public_source,
                )
            )
            return None
        await _report_plan_review_status(
            issue,
            workspace,
            context,
            (
                _plan_review_attempt_status(context.plan_review_attempt)
                if context.plan_review_attempt is not None
                else "🔎 Rechecking the approved plan against the current checkout."
            ),
        )
        reviewed_issue = await review_approved_plan(
            context.client,
            context.review_plan,
            workspace=workspace.folder,
            board=board,
            issue_id=issue.id,
            identifier=issue.identifier,
            title=issue.title,
            url=issue.url,
            description=issue.description,
            updated_at=issue.updated_at,
            public_source=context.public_source,
            attempt=context.plan_review_attempt or 1,
        )
        if reviewed_issue is None:
            await _report_plan_review_status(
                issue,
                workspace,
                context,
                "⚠️ Plan check did not admit work. See Linear for the updated plan or error.",
            )
            return None
        refreshed_issue = DecisionIssue.from_payload(reviewed_issue)
        if refreshed_issue is None:
            raise ValueError("Reviewed Linear issue no longer belongs to a project")
        issue = refreshed_issue
        await _report_plan_review_status(
            issue,
            workspace,
            context,
            "✅ Plan check passed. Starting work.",
        )
    task = await _task_for_issue(
        issue,
        workspace,
        context.observed_at,
        _TaskAdmission(status=HUMAN_APPROVED_STATUS, public_source=context.public_source),
    )
    try:
        execution = await acquire_work_item_lease(
            cast("LinearClient", context.client),
            WorkItemLeaseRequest(
                workspace=workspace.folder,
                issue_id=issue.id,
                request_id=f"{task.id}:lease",
                initiated_by=_CONTROLLER_INITIATOR,
                task_id=task.id,
                board=board,
            ),
        )
    except WorkItemClaimConflictError:
        return None
    if execution.status is not WorkItemExecutionStatus.IN_PROGRESS:
        return None
    active_task, admitted = await ensure_task_active(task, observed_at=context.observed_at)
    await _bind_execution_task(execution, active_task)
    return active_task if admitted else None


async def admit_decision_issue(
    issue: DecisionIssue,
    workspace: WorkspaceLike,
    board: LinearWorkspaceBoard,
    status: str,
    context: DecisionAdmission,
) -> ScheduledTask | None:
    """Admit or recover one status-classified managed Linear issue."""
    if status == "in_progress":
        return await _admit_in_progress_issue(issue, workspace, context)
    if status == FOLLOW_UPS_STATUS:
        return await _admit_follow_ups_issue(issue, workspace, context)
    return await _admit_human_approved_issue(issue, workspace, board, context)
