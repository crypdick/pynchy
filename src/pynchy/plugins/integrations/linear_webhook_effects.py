"""Trusted host effects for authenticated Linear webhook deliveries."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves configured webhook callbacks at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass, replace
from typing import Any

import aiohttp

from pynchy.conversation.api import conversation_runtime_lock
from pynchy.identifiers import GroupFolder
from pynchy.logger import logger
from pynchy.plugins.api import (
    WebhookConversation,
    WebhookEvent,
    WebhookLifecycleDelivery,
    WebhookProcessingError,
)
from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_statuses import (
    AWAITING_PLAN_APPROVAL_STATUS,
    HUMAN_APPROVED_STATUS,
    READY_FOR_PLANNING_STATUS,
)
from pynchy.plugins.integrations.linear_work_item_completion import (
    complete_reviewed_work_item,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    WorkItemLeaseRequest,
    acquire_human_started_work_item_lease,
    linear_client,
    state_id,
    workspace_issue,
)
from pynchy.work_items.api import (
    WorkItemClaimConflictError,
    WorkItemExecutionStatus,
)

_TERMINAL_STATE_BLOCKER = (
    "Linear reported a terminal state before this managed execution finished. "
    "Pynchy cancelled the local attempt without changing the provider issue."
)
_CANCELLABLE_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        WorkItemExecutionStatus.CLAIMING,
        WorkItemExecutionStatus.IN_PROGRESS,
        WorkItemExecutionStatus.AWAITING_REVIEW,
        WorkItemExecutionStatus.FOLLOW_UPS,
        WorkItemExecutionStatus.BLOCKED,
        WorkItemExecutionStatus.UNKNOWN,
    }
)
_PROJECT_ASSIGNMENT_UPDATE_FIELDS = frozenset({"addedtoprojectat", "projectid", "updatedat"})


@dataclass(frozen=True)
class LinearWebhookEffectsRuntime:
    """Durable control and execution operations selected during composition."""

    resolve_conversation: Callable[..., Awaitable[Any]]
    control_state_matches: Callable[..., Awaitable[bool]]
    apply_control_state: Callable[..., Awaitable[bool]]
    get_execution_for_issue: Callable[..., Awaitable[Any]]
    cancel_execution: Callable[..., Awaitable[object]]
    cancel_execution_if_lifecycle_current: Callable[..., Awaitable[object]]
    get_active_execution: Callable[[str], Awaitable[Any]]
    start_work_item_reconciliation: Callable[[], Awaitable[None]]


_runtime: LinearWebhookEffectsRuntime | None = None


def configure_linear_webhook_effects_runtime(runtime: LinearWebhookEffectsRuntime) -> None:
    """Set durable effects used after authenticated Linear webhook admission."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> LinearWebhookEffectsRuntime:
    if _runtime is None:
        raise RuntimeError("Linear webhook effects runtime has not been configured")
    return _runtime


async def process_linear_webhook_event(event: WebhookEvent) -> WebhookEvent:
    """Apply host-owned authorization and leasing before ordinary admission."""
    conversation = event.conversation
    if conversation is None:
        return event
    if event.lifecycle is not None:
        # Terminal ingress retires routed work before ordinary webhook admission.
        return event
    runtime_workspace = conversation.workspace
    if runtime_workspace is None:
        raise WebhookProcessingError("Linear host effect has no resolved workspace")
    if _is_project_assignment_only_update(event):
        # Preparation already resolved the issue's durable workspace ownership.
        # Project placement alone carries no agent work.
        return replace(
            event,
            instructions=None,
            external_context=None,
            ignored_reason="issue_project_assignment_does_not_wake_agent",
            conversation=None,
        )
    controller_workspace = conversation.controller_workspace or runtime_workspace
    try:
        if not await _reopen_verified_conversation_control(conversation, runtime_workspace):
            return _stale_linear_control_state_event(event)
        if (
            event.event_type == "Issue"
            and event.action == "update"
            and event.ignored_reason is None
        ):
            return await _process_linear_issue_update(
                event,
                conversation,
                runtime_workspace,
                controller_workspace,
            )
    except (
        aiohttp.ClientError,
        LinearBoardError,
        LinearError,
        TimeoutError,
        ValueError,
        WorkItemClaimConflictError,
    ) as exc:
        raise WebhookProcessingError(str(exc)) from exc
    return event


async def _process_linear_issue_update(
    event: WebhookEvent,
    conversation: WebhookConversation,
    runtime_workspace: str,
    controller_workspace: str,
) -> WebhookEvent:
    """Fence controller work after durable provider-state admission."""
    runtime = _configured_runtime()
    resolved = await runtime.resolve_conversation(
        conversation.subject,
        GroupFolder(runtime_workspace),
    )
    async with conversation_runtime_lock(resolved.id):
        if not await runtime.control_state_matches(
            resolved.id,
            closed=False,
            control_state_revision=conversation.control_state_revision,
        ):
            return _stale_linear_control_state_event(event)
        if not await _controller_owns_event(event, controller_workspace):
            return event
    return replace(
        event,
        instructions=None,
        external_context=None,
        ignored_reason="work_item_execution_owned_by_controller",
        # Admission must reconcile the current open control, but this ignored
        # event still cannot enter the routed agent-turn FIFO.
        conversation=conversation,
    )


def _is_project_assignment_only_update(event: WebhookEvent) -> bool:
    changed_fields = frozenset(field.casefold() for field in event.changed_fields)
    return "projectid" in changed_fields and changed_fields <= _PROJECT_ASSIGNMENT_UPDATE_FIELDS


def _stale_linear_control_state_event(event: WebhookEvent) -> WebhookEvent:
    """Suppress an event superseded by a newer provider control state."""
    return replace(
        event,
        instructions=None,
        external_context=None,
        ignored_reason="stale_linear_control_state",
        conversation=None,
        effect_evidence=None,
    )


async def _reopen_verified_conversation_control(
    conversation: WebhookConversation,
    workspace: str,
) -> bool:
    """Open durable control only when Linear supplied a typed nonterminal state."""
    if conversation.control_closed is not False:
        return True
    if conversation.control_state_revision is None:
        raise WebhookProcessingError("Linear nonterminal control state lacks updatedAt")
    runtime = _configured_runtime()
    resolved = await runtime.resolve_conversation(conversation.subject, GroupFolder(workspace))
    return await runtime.apply_control_state(
        resolved.id,
        closed=False,
        control_state_revision=conversation.control_state_revision,
    )


async def process_linear_webhook_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
    """Complete exact managed Done; locally retire every other terminal state.

    Terminal ingress already stopped routed runtime work. Persisted callback
    state decides whether this effect completes managed work or only cancels
    local execution; it never mutates another terminal provider state.
    """
    context = delivery.context
    callback_state = context.get("linear_state_id") if context is not None else None
    managed_done_state = (
        context.get("linear_managed_done_state_id") if context is not None else None
    )
    if (
        not isinstance(callback_state, str)
        or not callback_state
        or not isinstance(managed_done_state, str)
        or not managed_done_state
    ):
        return
    workspace = str(delivery.workspace)
    controller_workspace = (
        context.get("linear_controller_workspace", workspace) if context is not None else workspace
    )
    if not isinstance(controller_workspace, str) or not controller_workspace:
        return
    if callback_state == managed_done_state:
        if delivery.lifecycle_fence is None:
            await complete_reviewed_work_item(
                workspace,
                delivery.subject_id,
                str(delivery.identity.delivery_id),
                controller_workspace=controller_workspace,
            )
        else:
            await complete_reviewed_work_item(
                workspace,
                delivery.subject_id,
                str(delivery.identity.delivery_id),
                lifecycle_fence=delivery.lifecycle_fence,
                controller_workspace=controller_workspace,
            )
        return
    runtime = _configured_runtime()
    execution = await runtime.get_execution_for_issue(delivery.subject_id, workspace=workspace)
    if execution is not None and execution.status in _CANCELLABLE_TERMINAL_EXECUTION_STATUSES:
        if delivery.lifecycle_fence is None:
            await runtime.cancel_execution(execution.id, blocker=_TERMINAL_STATE_BLOCKER)
        else:
            await runtime.cancel_execution_if_lifecycle_current(
                execution.id,
                blocker=_TERMINAL_STATE_BLOCKER,
                lifecycle_fence=delivery.lifecycle_fence,
            )


async def _controller_owns_event(event: WebhookEvent, workspace: str) -> bool:
    """Lease newly approved work or recognize an existing controller lease."""
    async with linear_client(workspace=workspace) as client:
        issue, board = await workspace_issue(client, workspace, event.subject_id)
        current_state_id = state_id(issue)
        planning_state_ids = {
            state_id(board.states[READY_FOR_PLANNING_STATUS]),
            state_id(board.states[AWAITING_PLAN_APPROVAL_STATUS]),
        }
        if current_state_id in planning_state_ids:
            return True
        approved_state_id = state_id(board.states[HUMAN_APPROVED_STATUS])
        in_progress_state_id = state_id(board.states["in_progress"])
        runtime = _configured_runtime()
        existing = await runtime.get_active_execution(event.subject_id)
        if existing is not None:
            return current_state_id in {approved_state_id, in_progress_state_id}
        latest = await runtime.get_execution_for_issue(event.subject_id, workspace=workspace)
        if (
            latest is not None
            and latest.status is WorkItemExecutionStatus.CANCELLED
            and current_state_id == state_id(board.states["blocked"])
        ):
            # A context reset deliberately leaves this issue dormant. Only a
            # later Human Approved transition may acquire a fresh execution.
            return True
        if current_state_id != in_progress_state_id:
            return await _owns_nonprogress_controller_state(
                event,
                workspace,
                approved=current_state_id == approved_state_id,
            )
        actor = event.actor
        if not _is_human_state_transition(event) or actor is None:
            # In Progress is controller-owned even when its lease invariant
            # is broken. Routing this update to an ordinary conversation
            # would start competing work and could resume an unrelated
            # interactive provider session.
            logger.warning(
                "Unleased Linear In Progress update lacks human transition provenance",
                workspace=workspace,
                issue_id=event.subject_id,
                delivery_id=event.delivery_id,
            )
            return True
        execution = await acquire_human_started_work_item_lease(
            client,
            WorkItemLeaseRequest(
                workspace=workspace,
                issue_id=event.subject_id,
                request_id=f"linear-webhook:{event.delivery_id}:lease",
                initiated_by=(f"linear-webhook:{event.delivery_id}:user:{actor.id}"),
            ),
        )
    if execution.status is not WorkItemExecutionStatus.IN_PROGRESS:
        raise WebhookProcessingError("Linear execution lease did not become active")
    return True


async def _owns_nonprogress_controller_state(
    event: WebhookEvent,
    workspace: str,
    *,
    approved: bool,
) -> bool:
    """Wake review discovery for approved work while retaining the poll backstop."""
    if not approved:
        return False
    try:
        await _configured_runtime().start_work_item_reconciliation()
    except Exception:  # noqa: BLE001 - the periodic poll remains the durable backstop.
        logger.exception(
            "Immediate Linear work item reconciliation failed",
            workspace=workspace,
            issue_id=event.subject_id,
        )
    return True


def _is_human_state_transition(event: WebhookEvent) -> bool:
    actor = event.actor
    if event.action != "update" or actor is None or actor.kind.casefold() != "user":
        return False
    return any(field.casefold() in {"state", "stateid"} for field in event.changed_fields)
