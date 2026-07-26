"""Trusted host effects for authenticated Linear webhook deliveries."""

from __future__ import annotations

from dataclasses import replace

import aiohttp

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
    acquire_work_item_lease,
    linear_client,
    state_id,
    workspace_issue,
)
from pynchy.plugins.webhooks import (
    WebhookEvent,
    WebhookLifecycleDelivery,
    WebhookProcessingError,
)
from pynchy.state import (
    WorkItemClaimConflictError,
    get_active_work_item_execution,
    get_work_item_execution_for_issue,
    resolve_conversation,
)
from pynchy.types import GroupFolder, WorkItemExecutionStatus


async def process_linear_webhook_event(event: WebhookEvent) -> WebhookEvent:
    """Apply host-owned authorization and leasing before ordinary admission."""
    conversation = event.conversation
    if (
        event.event_type != "Issue"
        or event.action not in {"create", "update"}
        or conversation is None
    ):
        return event
    if event.lifecycle is not None:
        # Terminal callbacks are completed at their durable FIFO head.
        return event
    workspace = conversation.workspace
    if workspace is None:
        raise WebhookProcessingError("Linear host effect has no resolved workspace")
    try:
        if await _controller_owns_event(event, workspace):
            # The periodic controller owns planning and authorized execution.
            # Persist its routed identity without admitting a second agent turn.
            # The controller task binds to this same conversation runtime.
            await resolve_conversation(conversation.subject, GroupFolder(workspace))
            return replace(
                event,
                instructions=None,
                external_context=None,
                ignored_reason="work_item_execution_owned_by_controller",
                conversation=None,
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


async def process_linear_webhook_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
    """Complete reviewed work only for the persisted managed-Done state.

    A terminal callback can wait behind an earlier human delivery, so the
    callback's parsed state ID is the durable fact used for this decision.  Do
    not infer completion from the issue's mutable state when the FIFO reaches
    this delivery.
    """
    context = delivery.context
    state = context.get("linear_state_id") if context is not None else None
    if not isinstance(state, str) or not state:
        return
    workspace = str(delivery.workspace)
    try:
        async with linear_client(workspace=workspace) as client:
            _issue, board = await workspace_issue(client, workspace, delivery.subject_id)
            managed_done_state_id = state_id(board.states["done"])
        if state != managed_done_state_id:
            return
        await complete_reviewed_work_item(
            workspace,
            delivery.subject_id,
            str(delivery.identity.delivery_id),
        )
    except (
        aiohttp.ClientError,
        LinearBoardError,
        LinearError,
        TimeoutError,
        ValueError,
    ) as exc:
        raise WebhookProcessingError(str(exc)) from exc


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
        if current_state_id == in_progress_state_id:
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
        elif current_state_id == approved_state_id:
            execution = await acquire_work_item_lease(
                client,
                WorkItemLeaseRequest(
                    workspace=workspace,
                    issue_id=event.subject_id,
                    request_id=f"linear-webhook:{event.delivery_id}:lease",
                    initiated_by=f"linear-webhook:{event.delivery_id}",
                ),
            )
        else:
            return False
    if execution.status is not WorkItemExecutionStatus.IN_PROGRESS:
        raise WebhookProcessingError("Linear execution lease did not become active")
    return True


def _is_human_state_transition(event: WebhookEvent) -> bool:
    actor = event.actor
    if event.action != "update" or actor is None or actor.kind.casefold() != "user":
        return False
    return any(field.casefold() in {"state", "stateid"} for field in event.changed_fields)
