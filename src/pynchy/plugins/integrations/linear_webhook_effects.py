"""Trusted host effects for authenticated Linear webhook deliveries."""

from __future__ import annotations

from dataclasses import replace

import aiohttp

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
    acquire_work_item_lease,
    linear_client,
    state_id,
    workspace_issue,
)
from pynchy.plugins.webhooks import WebhookEvent, WebhookProcessingError
from pynchy.state import WorkItemClaimConflictError, get_active_work_item_execution
from pynchy.types import WorkItemExecutionStatus


async def process_linear_webhook_event(event: WebhookEvent) -> WebhookEvent:
    """Apply host-owned authorization, leasing, and completion bookkeeping."""
    conversation = event.conversation
    if (
        event.event_type != "Issue"
        or event.action not in {"create", "update"}
        or conversation is None
    ):
        return event
    workspace = conversation.workspace
    if workspace is None:
        raise WebhookProcessingError("Linear host effect has no resolved workspace")
    try:
        if conversation.control_closed is True:
            await complete_reviewed_work_item(workspace, event.subject_id, event.delivery_id)
            return event
        if await _controller_owns_event(event, workspace):
            # The periodic controller owns planning and authorized execution.
            # Admitting the same issue update as an ordinary conversation turn
            # would race a second agent against that durable task.
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
        if current_state_id != approved_state_id:
            return False
        execution = await acquire_work_item_lease(
            client,
            WorkItemLeaseRequest(
                workspace=workspace,
                issue_id=event.subject_id,
                request_id=f"linear-webhook:{event.delivery_id}:lease",
                initiated_by=f"linear-webhook:{event.delivery_id}",
            ),
        )
    if execution.status is not WorkItemExecutionStatus.IN_PROGRESS:
        raise WebhookProcessingError("Linear execution lease did not become active")
    return True
