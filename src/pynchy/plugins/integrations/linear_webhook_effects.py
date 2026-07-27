"""Trusted host effects for authenticated Linear webhook deliveries."""

from __future__ import annotations

from dataclasses import replace

import aiohttp

from pynchy.conversation.dispatch import conversation_runtime_lock
from pynchy.logger import logger
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
from pynchy.plugins.webhooks import (
    WebhookConversation,
    WebhookEvent,
    WebhookLifecycleDelivery,
    WebhookProcessingError,
)
from pynchy.state import (
    WorkItemClaimConflictError,
    apply_conversation_control_state,
    cancel_work_item_execution,
    cancel_work_item_execution_if_lifecycle_current,
    conversation_control_state_matches,
    get_active_work_item_execution,
    get_work_item_execution_for_issue,
    resolve_conversation,
)
from pynchy.types import GroupFolder, WorkItemExecutionStatus

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


async def process_linear_webhook_event(event: WebhookEvent) -> WebhookEvent:
    """Apply host-owned authorization and leasing before ordinary admission."""
    conversation = event.conversation
    if conversation is None:
        return event
    if event.lifecycle is not None:
        # Terminal ingress retires routed work before ordinary webhook admission.
        return event
    workspace = conversation.workspace
    if workspace is None:
        raise WebhookProcessingError("Linear host effect has no resolved workspace")
    try:
        if not await _reopen_verified_conversation_control(conversation, workspace):
            return _stale_linear_control_state_event(event)
        if (
            event.event_type == "Issue"
            and event.action == "update"
            and event.ignored_reason is None
        ):
            return await _process_linear_issue_update(event, conversation, workspace)
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
    workspace: str,
) -> WebhookEvent:
    """Fence controller work after durable provider-state admission."""
    resolved = await resolve_conversation(conversation.subject, GroupFolder(workspace))
    async with conversation_runtime_lock(resolved.id):
        if not await conversation_control_state_matches(
            resolved.id,
            closed=False,
            control_state_revision=conversation.control_state_revision,
        ):
            return _stale_linear_control_state_event(event)
        if not await _controller_owns_event(event, workspace):
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
    resolved = await resolve_conversation(conversation.subject, GroupFolder(workspace))
    return await apply_conversation_control_state(
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
    if callback_state == managed_done_state:
        if delivery.lifecycle_fence is None:
            await complete_reviewed_work_item(
                workspace,
                delivery.subject_id,
                str(delivery.identity.delivery_id),
            )
        else:
            await complete_reviewed_work_item(
                workspace,
                delivery.subject_id,
                str(delivery.identity.delivery_id),
                lifecycle_fence=delivery.lifecycle_fence,
            )
        return
    execution = await get_work_item_execution_for_issue(delivery.subject_id, workspace=workspace)
    if execution is not None and execution.status in _CANCELLABLE_TERMINAL_EXECUTION_STATUSES:
        if delivery.lifecycle_fence is None:
            await cancel_work_item_execution(execution.id, blocker=_TERMINAL_STATE_BLOCKER)
        else:
            await cancel_work_item_execution_if_lifecycle_current(
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
        existing = await get_active_work_item_execution(event.subject_id)
        if existing is not None:
            if existing.workspace != workspace:
                raise WorkItemClaimConflictError(existing)
            return current_state_id in {approved_state_id, in_progress_state_id}
        latest = await get_work_item_execution_for_issue(event.subject_id, workspace=workspace)
        if (
            latest is not None
            and latest.status is WorkItemExecutionStatus.CANCELLED
            and current_state_id == state_id(board.states["blocked"])
        ):
            # A context reset deliberately leaves this issue dormant. Only a
            # later Human Approved transition may acquire a fresh execution.
            return True
        if current_state_id != in_progress_state_id:
            # The periodic controller reviews any stored plan before it leases
            # approved work. Webhook admission must not bypass that boundary.
            return current_state_id == approved_state_id
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


def _is_human_state_transition(event: WebhookEvent) -> bool:
    actor = event.actor
    if event.action != "update" or actor is None or actor.kind.casefold() != "user":
        return False
    return any(field.casefold() in {"state", "stateid"} for field in event.changed_fields)
