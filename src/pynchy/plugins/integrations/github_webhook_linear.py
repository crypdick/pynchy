"""Route actionable GitHub PR feedback into linked Linear conversations."""

from __future__ import annotations

from collections.abc import Mapping

import aiohttp

from pynchy.plugins.api import (
    WebhookConversation,
    WebhookEvent,
    WebhookProcessingError,
)
from pynchy.plugins.integrations.github_webhook_models import (  # noqa: TC001 - beartype resolves this annotation at runtime.
    GitHubWebhookRouteConfig,
)
from pynchy.plugins.integrations.linear_accounts import linear_account_for_workspace
from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_conversation_identity import (
    resolve_linear_issue_conversation,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearWorkspaceIssueError,
    linear_client,
    workspace_issue,
)


def _fallback_notification(event: WebhookEvent) -> WebhookEvent:
    context = event.external_context
    message = context.get("fallback_host_message") if isinstance(context, Mapping) else None
    if not isinstance(message, str):
        raise WebhookProcessingError("GitHub actionable event lacks its fallback notification")
    return WebhookEvent(
        delivery_id=event.delivery_id,
        event_type=event.event_type,
        action=event.action,
        subject_id=event.subject_id,
        occurred_at=event.occurred_at,
        instructions=None,
        external_context=None,
        host_message=message,
    )


async def _linked_issue_for_pr(
    config: GitHubWebhookRouteConfig,
    pr_url: str,
) -> tuple[str, dict[str, object]] | None:
    async with linear_client(workspace=config.workspace) as client:
        attachments = await client.find_issues_by_attachment_url(pr_url)
        if len(attachments) != 1:
            return None
        attachment_issue = attachments[0].get("issue")
        issue_id = attachment_issue.get("id") if isinstance(attachment_issue, dict) else None
        if not isinstance(issue_id, str):
            return None
        issue, _board = await workspace_issue(client, config.workspace, issue_id)
        return issue_id, issue


async def prepare_github_webhook_event(  # noqa: PLR0911 - each branch preserves a closed webhook disposition.
    event: WebhookEvent,
    *,
    config: GitHubWebhookRouteConfig,
) -> WebhookEvent:
    """Route linked review work into its canonical Linear issue conversation."""
    if (
        event.event_type
        not in {
            "issue_comment",
            "pull_request_review",
            "pull_request_review_comment",
            "check_run",
        }
        or event.instructions is None
    ):
        return event
    account = linear_account_for_workspace(config.workspace)
    if account is None:
        return _fallback_notification(event)
    context = event.external_context
    if not isinstance(context, Mapping):
        return _fallback_notification(event)
    pr_url = context.get("pull_request_url")
    if not isinstance(pr_url, str):
        return _fallback_notification(event)
    try:
        linked_issue = await _linked_issue_for_pr(config, pr_url)
    except LinearWorkspaceIssueError:
        return _fallback_notification(event)
    except (aiohttp.ClientError, LinearBoardError, LinearError, TimeoutError, ValueError) as exc:
        raise WebhookProcessingError(str(exc)) from exc
    if linked_issue is None:
        return _fallback_notification(event)
    issue_id, issue = linked_issue
    conversation = await resolve_linear_issue_conversation(
        issue_id,
        config.workspace,
        account.name,
    )
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
        external_context={
            key: value for key, value in context.items() if key != "fallback_host_message"
        },
        conversation=WebhookConversation(
            subject=conversation.subject,
            control_title=control_title[:100],
            workspace=config.workspace,
            public_source=True,
        ),
    )
