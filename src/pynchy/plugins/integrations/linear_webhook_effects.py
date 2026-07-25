"""Trusted host effects for authenticated Linear webhook deliveries."""

from __future__ import annotations

import aiohttp

from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_statuses import HUMAN_APPROVED_STATUS
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
from pynchy.state import WorkItemClaimConflictError
from pynchy.types import WorkItemExecutionStatus


async def process_linear_webhook_event(event: WebhookEvent) -> None:
    """Apply host-owned authorization, leasing, and completion bookkeeping."""
    conversation = event.conversation
    if (
        event.event_type != "Issue"
        or event.action not in {"create", "update"}
        or conversation is None
    ):
        return
    workspace = conversation.workspace
    if workspace is None:
        raise WebhookProcessingError("Linear host effect has no resolved workspace")
    try:
        if conversation.control_closed is True:
            await complete_reviewed_work_item(workspace, event.subject_id, event.delivery_id)
        else:
            await _acquire_execution_lease(event, workspace)
    except (
        aiohttp.ClientError,
        LinearBoardError,
        LinearError,
        TimeoutError,
        ValueError,
        WorkItemClaimConflictError,
    ) as exc:
        raise WebhookProcessingError(str(exc)) from exc


async def _acquire_execution_lease(event: WebhookEvent, workspace: str) -> None:
    """Lease an authorized issue before its routed agent turn is admitted."""
    async with linear_client(workspace=workspace) as client:
        issue, board = await workspace_issue(client, workspace, event.subject_id)
        if state_id(issue) != state_id(board.states[HUMAN_APPROVED_STATUS]):
            return
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
