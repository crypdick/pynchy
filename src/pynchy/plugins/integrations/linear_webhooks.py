"""Authenticated Linear webhook routes and workspace admission."""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves webhook runtime callbacks at runtime.
    Mapping,  # noqa: TC003 - beartype resolves webhook parser annotations at runtime.
)
from dataclasses import dataclass, replace
from datetime import (
    datetime,  # noqa: TC003 - beartype resolves webhook parser annotations at runtime.
)
from functools import partial
from typing import Any

import aiohttp

from pynchy.conversation.api import parent_workspace_name
from pynchy.plugins.api import (
    WebhookEvent,
    WebhookLifecycle,
    WebhookProcessingError,
    WebhookRoute,
)
from pynchy.plugins.integrations.linear_accounts import (
    LinearAccount,  # noqa: TC001 - beartype resolves webhook runtime callbacks at runtime.
)
from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001 - beartype resolves workspace evidence.
    LinearWorkspaceBoard,
)
from pynchy.plugins.integrations.linear_boot import (
    linear_workspace_enabled,
    workspace_for_linear_project,
)
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_conversation_identity import (
    resolve_linear_issue_conversation,
)
from pynchy.plugins.integrations.linear_statuses import TERMINAL_STATE_TYPES
from pynchy.plugins.integrations.linear_webhook_config import (
    LinearPluginOptions,  # noqa: TC001 - beartype resolves webhook runtime callbacks at runtime.
    LinearWebhookRouteConfig,  # noqa: TC001 - beartype resolves webhook runtime callbacks at runtime.
)
from pynchy.plugins.integrations.linear_webhook_effects import (
    process_linear_webhook_event,
    process_linear_webhook_lifecycle,
)
from pynchy.plugins.integrations.linear_webhook_parser import (
    parse_linear_webhook as _parse_linear_webhook,
)
from pynchy.plugins.integrations.linear_webhook_prompts import (  # noqa: TC001 - beartype resolves webhook parser annotations at runtime.
    LinearWebhookPrompts,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearWorkspaceIssueError,
    linear_client,
    state_id,
    workspace_issue,
)
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves route validation annotations at runtime.
)


@dataclass(frozen=True)
class LinearWebhookRuntime:
    """Resolved route configuration selected during Linear plugin composition."""

    options: LinearPluginOptions
    prompts: LinearWebhookPrompts
    account_for_name: Callable[[str], LinearAccount]
    workspace_tools: Callable[[str], tuple[str, ...] | None]
    workspace_names_for_account: Callable[[str], tuple[str, ...]]


_runtime: LinearWebhookRuntime | None = None


def configure_linear_webhook_runtime(runtime: LinearWebhookRuntime) -> None:
    """Set resolved Linear webhook configuration before routes are collected."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> LinearWebhookRuntime:
    if _runtime is None:
        raise RuntimeError("Linear webhook runtime has not been configured")
    return _runtime


# allow: too-many-arguments - public parser keeps its authenticated transport boundary explicit.
def parse_linear_webhook(  # noqa: PLR0913
    raw_body: bytes,
    raw_headers: Mapping[str, str],
    secret: str,
    now: datetime,
    *,
    config: LinearWebhookRouteConfig,
    prompts: LinearWebhookPrompts | None = None,
) -> WebhookEvent:
    """Parse one delivery using the prompts resolved for the Linear runtime."""
    return _parse_linear_webhook(
        raw_body,
        raw_headers,
        secret,
        now,
        config=config,
        prompts=prompts or _configured_runtime().prompts,
    )


async def _event_workspace(
    event: WebhookEvent,
    config: LinearWebhookRouteConfig,
) -> tuple[str, dict[str, Any], LinearWorkspaceBoard]:
    async with linear_client(account_name=config.tool) as client:
        if config.workspace is not None:
            current_issue, board = await workspace_issue(client, config.workspace, event.subject_id)
            return config.workspace, current_issue, board
        issue = await client.get_issue(event.subject_id)
        project = issue.get("project") if issue is not None else None
        project_id = project.get("id") if isinstance(project, dict) else None
        workspace = (
            workspace_for_linear_project(project_id) if isinstance(project_id, str) else None
        )
        if workspace is None:
            raise LinearWorkspaceIssueError("Linear issue is not on a managed workspace board")
        resolved_issue, board = await workspace_issue(client, workspace, event.subject_id)
        return workspace, resolved_issue, board


def _typed_current_issue_state(issue: dict[str, Any]) -> tuple[str, bool, str] | None:
    """Return current state ID, terminal intent, and provider revision when typed."""
    state = issue.get("state")
    if not isinstance(state, dict):
        return None
    current_state_id = state.get("id")
    state_type = state.get("type")
    if (
        not isinstance(current_state_id, str)
        or not current_state_id
        or not isinstance(state_type, str)
        or not state_type
    ):
        return None
    revision = issue.get("updatedAt")
    if not isinstance(revision, str) or not revision:
        raise WebhookProcessingError("Linear issue current state lacks updatedAt")
    return current_state_id, state_type in TERMINAL_STATE_TYPES, revision


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
        workspace, issue, board = await _event_workspace(event, config)
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
    conversation = await resolve_linear_issue_conversation(
        event.subject_id,
        workspace,
        config.tool,
    )
    current_state = _typed_current_issue_state(issue)
    if current_state is not None and event.lifecycle is not None:
        _current_state_id, current_terminal, current_revision = current_state
        if not current_terminal:
            # Keep the canonical conversation so the trusted effect can reopen
            # its durable control without replaying this stale terminal callback.
            return replace(
                event,
                instructions=None,
                external_context=None,
                ignored_reason="stale_terminal_issue_state",
                conversation=replace(
                    event.conversation,
                    subject=conversation.subject,
                    workspace=str(conversation.workspace),
                    controller_workspace=workspace,
                    public_source=public_source,
                    control_closed=False,
                    control_state_revision=current_revision,
                ),
                lifecycle=None,
                effect_evidence=None,
            )

    prepared_conversation = event.conversation
    if current_state is not None:
        current_state_id, current_terminal, current_revision = current_state
        control_state_revision = current_revision
        if current_terminal:
            return replace(
                event,
                instructions=None,
                external_context=None,
                ignored_reason=None,
                conversation=replace(
                    prepared_conversation,
                    subject=conversation.subject,
                    workspace=str(conversation.workspace),
                    controller_workspace=workspace,
                    public_source=public_source,
                    control_closed=True,
                    control_state_revision=control_state_revision,
                ),
                lifecycle=WebhookLifecycle(
                    context={
                        "linear_state_id": current_state_id,
                        "linear_managed_done_state_id": state_id(board.states["done"]),
                        "linear_controller_workspace": workspace,
                    }
                ),
                effect_evidence=None,
            )
        prepared_conversation = replace(
            prepared_conversation,
            control_closed=False,
            control_state_revision=control_state_revision,
        )
    elif prepared_conversation.control_closed is False:
        # No typed current state means no proof that an older callback may reopen a control.
        prepared_conversation = replace(prepared_conversation, control_closed=None)

    lifecycle = event.lifecycle
    if lifecycle is not None:
        context = dict(lifecycle.context or {})
        context["linear_managed_done_state_id"] = state_id(board.states["done"])
        context["linear_controller_workspace"] = workspace
        lifecycle = replace(lifecycle, context=context)
    return replace(
        event,
        conversation=replace(
            prepared_conversation,
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
    runtime = _configured_runtime()
    account = runtime.account_for_name(config.tool)
    tools = runtime.workspace_tools(workspace.folder)
    if tools is None or account.name not in tools:
        return f"requires its workspace to select Linear account tool '{account.name}'"
    if account.config.public_source == "forbidden":
        return "requires its Linear account tool to permit source content"
    return None


def linear_webhook_routes() -> tuple[WebhookRoute, ...]:
    """Parse plugin options and return configured Linear webhook routes."""
    runtime = _configured_runtime()
    routes: list[WebhookRoute] = []
    for config in runtime.options.webhook_routes:
        account = runtime.account_for_name(config.tool)
        routes.append(
            WebhookRoute(
                provider="linear",
                name=config.name,
                workspace=config.workspace,
                secret_env=config.secret_env,
                parse=partial(parse_linear_webhook, config=config, prompts=runtime.prompts),
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
                    runtime.workspace_names_for_account(config.tool)
                    if config.workspace is None
                    else ()
                ),
                allow_admin_workspaces=config.workspace is None,
                process_lifecycle=process_linear_webhook_lifecycle,
            )
        )
    return tuple(routes)
