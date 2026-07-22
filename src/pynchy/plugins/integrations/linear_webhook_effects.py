"""Trusted host effects for authenticated Linear webhook deliveries."""

from __future__ import annotations

import aiohttp

from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_work_item_completion import (
    complete_reviewed_work_item,
)
from pynchy.plugins.webhooks import WebhookEvent, WebhookProcessingError


async def process_linear_webhook_event(event: WebhookEvent) -> None:
    """Finalize a linked execution after an authenticated Linear Done update."""
    conversation = event.conversation
    if (
        event.event_type != "Issue"
        or event.action not in {"create", "update"}
        or conversation is None
        or conversation.control_closed is not True
    ):
        return
    workspace = conversation.workspace
    if workspace is None:
        raise WebhookProcessingError("Linear review completion has no resolved workspace")
    try:
        await complete_reviewed_work_item(workspace, event.subject_id, event.delivery_id)
    except (aiohttp.ClientError, LinearBoardError, LinearError, TimeoutError, ValueError) as exc:
        raise WebhookProcessingError(str(exc)) from exc
