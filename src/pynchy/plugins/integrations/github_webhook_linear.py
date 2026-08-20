"""Route GitHub PR updates into existing linked Linear conversations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import aiohttp

from pynchy.plugins.api import (
    WebhookConversation,
    WebhookEvent,
    WebhookProcessingError,
)
from pynchy.plugins.integrations.github_webhook_models import (  # noqa: TC001 - beartype resolves this annotation at runtime.
    GitHubWebhookRouteConfig,
)
from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_boot import workspace_for_linear_project
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_conversation_identity import (
    find_linear_issue_control_conversation,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearWorkspaceIssueError,
    linear_client,
    workspace_issue,
)


def _ignored(event: WebhookEvent, reason: str) -> WebhookEvent:
    return replace(
        event,
        instructions=None,
        external_context=None,
        host_message=None,
        conversation=None,
        ignored_reason=reason,
    )


def _pull_request_url(event: WebhookEvent, repository: str) -> str | None:
    context = event.external_context
    url = context.get("pull_request_url") if isinstance(context, Mapping) else None
    if isinstance(url, str):
        return url
    if event.subject_id.isdecimal():
        return f"https://github.com/{repository}/pull/{event.subject_id}"
    return None


async def _linked_issue_for_pr(
    config: GitHubWebhookRouteConfig,
    pr_url: str,
) -> tuple[str, str, dict[str, object]] | None:
    async with linear_client(account_name=config.tool) as client:
        attachments = await client.find_issues_by_attachment_url(pr_url)
        if len(attachments) != 1:
            return None
        attachment_issue = attachments[0].get("issue")
        issue_id = attachment_issue.get("id") if isinstance(attachment_issue, dict) else None
        if not isinstance(issue_id, str):
            return None
        project = attachment_issue.get("project")
        project_id = project.get("id") if isinstance(project, dict) else None
        workspace = (
            workspace_for_linear_project(project_id) if isinstance(project_id, str) else None
        )
        if workspace is None:
            return None
        issue, _board = await workspace_issue(client, workspace, issue_id)
        return issue_id, workspace, issue


async def prepare_github_webhook_event(  # noqa: PLR0911 - each branch preserves a closed webhook disposition.
    event: WebhookEvent,
    *,
    config: GitHubWebhookRouteConfig,
) -> WebhookEvent:
    """Route GitHub updates only into an existing managed Linear control."""
    if event.ignored_reason is not None:
        return event
    if event.event_type not in {
        "issue_comment",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "check_run",
    }:
        return event
    pr_url = _pull_request_url(event, config.repository)
    if pr_url is None:
        return _ignored(event, "github_event_has_no_single_pull_request")
    try:
        linked_issue = await _linked_issue_for_pr(config, pr_url)
    except LinearWorkspaceIssueError:
        return _ignored(event, "linear_issue_is_not_on_managed_board")
    except (aiohttp.ClientError, LinearBoardError, LinearError, TimeoutError, ValueError) as exc:
        raise WebhookProcessingError(str(exc)) from exc
    if linked_issue is None:
        return _ignored(event, "pull_request_has_no_managed_linear_issue")
    issue_id, workspace, issue = linked_issue
    controlled = await find_linear_issue_control_conversation(
        issue_id,
        workspace,
    )
    if controlled is None:
        return _ignored(event, "linear_issue_has_no_existing_control")
    conversation, binding = controlled
    identifier = issue.get("identifier")
    title = issue.get("title")
    control_title = (
        f"[{identifier}] {title}"
        if isinstance(identifier, str) and isinstance(title, str)
        else "Linear issue"
    )
    return WebhookEvent(
        delivery_id=event.delivery_id,
        event_type=event.event_type,
        action=event.action,
        subject_id=event.subject_id,
        occurred_at=event.occurred_at,
        instructions=event.instructions,
        external_context=event.external_context,
        host_message=event.host_message,
        conversation=WebhookConversation(
            subject=conversation.subject,
            control_title=control_title[:100],
            workspace=str(conversation.workspace),
            public_source=not config.allowed_senders,
            notification_jid=binding.thread_jid if event.host_message is not None else None,
        ),
    )
