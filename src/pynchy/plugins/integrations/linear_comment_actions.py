"""Host-owned Linear comment writes with durable self-echo evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pynchy.conversation.api import parent_workspace_name
from pynchy.plugins.api import ActionIntentDraft, ActionIntentReceipt
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearWorkspaceIssueError,
    linear_client,
    workspace_issue,
)

_SOURCE_GROUP_REQUIRED = "source_group is required"
_ISSUE_ID_REQUIRED = "issue_id is required"
_BODY_REQUIRED = "body is required"
_RECEIPT_REQUIRED = "Linear comment response lacks a durable receipt"


@dataclass(frozen=True)
class _CommentRequest:
    workspace: str
    issue_id: str
    body: str


def _required_text(data: dict[str, Any], key: str, message: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value


def _comment_request(data: dict[str, Any]) -> _CommentRequest:
    source_group = _required_text(data, "source_group", _SOURCE_GROUP_REQUIRED)
    return _CommentRequest(
        workspace=parent_workspace_name(source_group) or source_group,
        issue_id=_required_text(data, "issue_id", _ISSUE_ID_REQUIRED),
        body=_required_text(data, "body", _BODY_REQUIRED),
    )


def linear_comment_action_draft(data: dict[str, Any]) -> ActionIntentDraft:
    """Freeze the exact comment destination and body before sending it to Linear."""
    request = _comment_request(data)
    return ActionIntentDraft(
        recipient=f"linear:{request.workspace}:issue:{request.issue_id}",
        payload={"issue_id": request.issue_id, "body": request.body},
        summary=f"Comment on Linear issue {request.issue_id}",
    )


def linear_comment_action_receipt(response: dict[str, Any]) -> ActionIntentReceipt:
    """Accept only a provider response that includes the immutable comment identity."""
    comment = response.get("result")
    if not isinstance(comment, dict):
        raise TypeError(_RECEIPT_REQUIRED)
    comment_id = comment.get("id")
    if not isinstance(comment_id, str) or not comment_id:
        raise ValueError(_RECEIPT_REQUIRED)
    return ActionIntentReceipt(provider_request_id=comment_id, receipt=comment)


async def handle_create_comment(data: dict[str, Any]) -> dict[str, object]:
    """Write one workspace-owned comment after host policy and intent checks."""
    try:
        request = _comment_request(data)
        async with linear_client(workspace=request.workspace) as client:
            await workspace_issue(client, request.workspace, request.issue_id)
            comment = await client.create_comment(request.issue_id, request.body)
    except (LinearError, LinearWorkspaceIssueError, ValueError) as exc:
        return {"error": str(exc)}
    return {"result": comment}
