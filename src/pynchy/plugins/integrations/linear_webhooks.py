"""Authenticated Linear webhook routes and workspace admission."""

from __future__ import annotations

from dataclasses import replace
from functools import partial

import aiohttp

from pynchy.config import get_settings
from pynchy.config.workspace_names import parent_workspace_name
from pynchy.plugins.integrations.linear_accounts import linear_account
from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001, RUF100 - beartype resolves workspace evidence.
    LinearWorkspaceBoard,
)
from pynchy.plugins.integrations.linear_boot import (
    configured_linear_workspace_names,
    linear_workspace_enabled,
    workspace_for_linear_project,
)
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_conversation_identity import (
    resolve_linear_issue_conversation,
)
from pynchy.plugins.integrations.linear_webhook_config import (
    LinearPluginOptions,
    LinearWebhookRouteConfig,
)
from pynchy.plugins.integrations.linear_webhook_effects import (
    process_linear_webhook_event,
    process_linear_webhook_lifecycle,
)
from pynchy.plugins.integrations.linear_webhook_parser import parse_linear_webhook
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearWorkspaceIssueError,
    linear_client,
    state_id,
    workspace_issue,
)
from pynchy.plugins.webhooks import (
    WebhookEvent,
    WebhookProcessingError,
    WebhookRoute,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves route validation annotations at runtime.
    WorkspaceProfile,
)


async def _event_workspace(
    event: WebhookEvent,
    config: LinearWebhookRouteConfig,
) -> tuple[str, LinearWorkspaceBoard]:
    async with linear_client(account_name=config.tool) as client:
        if config.workspace is not None:
            _issue, board = await workspace_issue(client, config.workspace, event.subject_id)
            return config.workspace, board
        issue = await client.get_issue(event.subject_id)
        project = issue.get("project") if issue is not None else None
        project_id = project.get("id") if isinstance(project, dict) else None
        workspace = (
            workspace_for_linear_project(project_id) if isinstance(project_id, str) else None
        )
        if workspace is None:
            raise LinearWorkspaceIssueError("Linear issue is not on a managed workspace board")
        _issue, board = await workspace_issue(client, workspace, event.subject_id)
        return workspace, board


async def prepare_linear_webhook_event(
    event: WebhookEvent,
    *,
    config: LinearWebhookRouteConfig,
    public_source: bool = True,
) -> WebhookEvent:
    """Confirm workspace-board ownership before creating or waking an issue thread."""
    if event.conversation is None:
        return event
    try:
        workspace, board = await _event_workspace(event, config)
    except LinearWorkspaceIssueError:
        return replace(
            event,
            instructions=None,
            external_context=None,
            ignored_reason="issue_is_not_on_workspace_board",
            conversation=None,
            lifecycle=None,
        )
    except (aiohttp.ClientError, LinearBoardError, LinearError, TimeoutError, ValueError) as exc:
        raise WebhookProcessingError(str(exc)) from exc
    if event.conversation is None:
        return event
    conversation = await resolve_linear_issue_conversation(
        event.subject_id,
        workspace,
        config.tool,
    )
    lifecycle = event.lifecycle
    if lifecycle is not None:
        context = dict(lifecycle.context or {})
        context["linear_managed_done_state_id"] = state_id(board.states["done"])
        lifecycle = replace(lifecycle, context=context)
    return replace(
        event,
        conversation=replace(
            event.conversation,
            subject=conversation.subject,
            workspace=str(conversation.workspace),
            controller_workspace=workspace,
            public_source=public_source,
        ),
        lifecycle=lifecycle,
    )


def _validate_linear_workspace(
    workspace: WorkspaceProfile,
    *,
    config: LinearWebhookRouteConfig,
) -> str | None:
    if not linear_workspace_enabled(workspace):
        return "requires its workspace to select a Linear tool"
    if not workspace.jid.startswith("discord:channel:"):
        return "requires a Discord guild-channel workspace for issue controls"
    if parent_workspace_name(workspace.folder) is not None:
        return "requires a registered workspace root instead of a child conversation"
    settings = get_settings()
    account = linear_account(config.tool, settings)
    resolved = settings.resolved_workspace_config(workspace.folder)
    if resolved is None or account.name not in resolved.tools:
        return f"requires its workspace to select Linear account tool '{account.name}'"
    if account.config.public_source == "forbidden":
        return "requires its Linear account tool to permit source content"
    return None


def linear_webhook_routes() -> tuple[WebhookRoute, ...]:
    """Parse plugin options and return configured Linear webhook routes."""
    settings = get_settings()
    plugin = settings.plugins.get("linear")
    options = LinearPluginOptions.model_validate(plugin.options if plugin is not None else {})
    routes: list[WebhookRoute] = []
    for config in options.webhook_routes:
        account = linear_account(config.tool, settings)
        routes.append(
            WebhookRoute(
                provider="linear",
                name=config.name,
                workspace=config.workspace,
                secret_env=config.secret_env,
                parse=partial(parse_linear_webhook, config=config),
                public_source=account.config.public_source is not False,
                validate_workspace=partial(_validate_linear_workspace, config=config),
                max_body_bytes=config.max_body_bytes,
                rate_limit_requests=config.rate_limit_requests,
                rate_limit_window_seconds=config.rate_limit_window_seconds,
                prepare_event=partial(
                    prepare_linear_webhook_event,
                    config=config,
                    public_source=account.config.public_source is not False,
                ),
                process_event=process_linear_webhook_event,
                routes_conversations=True,
                candidate_workspaces=(
                    configured_linear_workspace_names(config.tool)
                    if config.workspace is None
                    else ()
                ),
                allow_admin_workspaces=config.workspace is None,
                process_lifecycle=process_linear_webhook_lifecycle,
            )
        )
    return tuple(routes)
