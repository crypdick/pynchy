"""Authenticated Linear webhook routes and closed event mapping."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves parser annotations at runtime.
    Mapping,
)
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal
from uuid import UUID

import aiohttp
from pydantic import BaseModel, Field, ValidationError, field_validator

from pynchy.config import get_settings
from pynchy.config.workspace_names import parent_workspace_name
from pynchy.conversation.models import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.plugins.integrations.linear_accounts import linear_account
from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_boot import (
    configured_linear_workspace_names,
    linear_workspace_enabled,
    workspace_for_linear_project,
)
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_statuses import LINEAR_TODO_STATUSES
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearWorkspaceIssueError,
    linear_client,
    workspace_issue,
)
from pynchy.plugins.webhooks import (
    WebhookAuthenticationError,
    WebhookConversation,
    WebhookEvent,
    WebhookPayloadError,
    WebhookProcessingError,
    WebhookRoute,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves route validation annotations at runtime.
    WorkspaceProfile,
)

# NOTE: Update docs/integrations/linear.md "Receive Linear callbacks" and
# docs/architecture/conversation-routing.md "Linear Issue Webhooks" if this
# event-admission or prompt contract changes.
_LINEAR_ISSUE_INSTRUCTIONS = (
    "The Linear issue bound to this thread changed. Read its current state and take "
    "appropriate action."
)
_LINEAR_ISSUE_URL = re.compile(r"/issue/([^/#?]+)", re.IGNORECASE)
_DISCORD_THREAD_TITLE_LIMIT = 100
_DONE_STATE_NAME = LINEAR_TODO_STATUSES["done"].name


class _LinearWebhookModel(BaseModel):
    model_config = {"extra": "ignore", "populate_by_name": True}


class LinearWebhookRouteConfig(_LinearWebhookModel):
    """Plugin-owned config for one Linear webhook subscription."""

    model_config = {"extra": "forbid"}

    name: str
    workspace: str | None = None
    tool: str = "linear"
    secret_env: str = "LINEAR_WEBHOOK_SECRET"  # noqa: S105, RUF100 - environment variable name, not a credential.
    organization_id: str | None = None
    timestamp_tolerance_seconds: int = 60
    max_body_bytes: int = 256 * 1024
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    @field_validator("name", "tool", "secret_env")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Linear webhook route text fields cannot be empty")
        return value

    @field_validator(
        "timestamp_tolerance_seconds",
        "max_body_bytes",
        "rate_limit_requests",
        "rate_limit_window_seconds",
    )
    @classmethod
    def validate_positive_limits(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Linear webhook limits must be positive")
        return value


class LinearPluginOptions(_LinearWebhookModel):
    """Typed transport parser for ``[plugins.linear.options]``."""

    model_config = {"extra": "forbid"}

    webhook_routes: tuple[LinearWebhookRouteConfig, ...] = ()


class _LinearActor(_LinearWebhookModel):
    id: str = ""
    type: str = ""
    name: str = ""


class _LinearWebhookPayload(_LinearWebhookModel):
    action: Literal["create", "update", "remove"]
    type: str
    actor: _LinearActor | None = None
    data: dict[str, Any]
    updated_from: dict[str, Any] | None = Field(default=None, alias="updatedFrom")
    url: str = ""
    created_at: str = Field(alias="createdAt")
    organization_id: str = Field(alias="organizationId", min_length=1)
    webhook_timestamp: int = Field(alias="webhookTimestamp")


def _header(headers: Mapping[str, str], name: str) -> str:
    normalized = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == normalized), "")


def _authenticate(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    now: datetime,
    config: LinearWebhookRouteConfig,
) -> tuple[str, int]:
    signature = _header(headers, "Linear-Signature")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature.casefold(), expected):
        raise WebhookAuthenticationError("Linear signature verification failed")

    delivery = _header(headers, "Linear-Delivery")
    try:
        delivery_id = str(UUID(delivery))
    except ValueError as exc:
        raise WebhookAuthenticationError("Linear delivery ID is missing or invalid") from exc

    raw_timestamp = _header(headers, "Linear-Timestamp")
    try:
        timestamp = int(raw_timestamp)
    except ValueError as exc:
        raise WebhookAuthenticationError("Linear timestamp is missing or invalid") from exc
    now_ms = int(now.astimezone(UTC).timestamp() * 1000)
    if abs(now_ms - timestamp) > config.timestamp_tolerance_seconds * 1000:
        raise WebhookAuthenticationError("Linear webhook timestamp is outside the replay window")
    return delivery_id, timestamp


def _parse_payload(raw_body: bytes, timestamp: int) -> _LinearWebhookPayload:
    try:
        raw_payload = json.loads(raw_body)
        payload = _LinearWebhookPayload.model_validate(raw_payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise WebhookPayloadError("Linear webhook payload does not match its schema") from exc
    if payload.webhook_timestamp != timestamp:
        raise WebhookPayloadError("Linear webhook header and body timestamps differ")
    return payload


def _required_data_text(payload: _LinearWebhookPayload, key: str) -> str:
    value = payload.data.get(key)
    if not isinstance(value, str) or not value:
        raise WebhookPayloadError(f"Linear {payload.type} payload is missing data.{key}")
    return value


def _ignored_event(
    payload: _LinearWebhookPayload,
    delivery_id: str,
    *,
    subject_id: str,
    reason: str,
) -> WebhookEvent:
    return WebhookEvent(
        delivery_id=delivery_id,
        event_type=payload.type,
        action=payload.action,
        subject_id=subject_id,
        occurred_at=payload.created_at,
        instructions=None,
        external_context=None,
        ignored_reason=reason,
    )


def _actor_name(payload: _LinearWebhookPayload) -> str:
    if payload.actor is None:
        return "Unknown"
    return payload.actor.name.strip() or payload.actor.id.strip() or "Unknown"


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _issue_display_fields(payload: _LinearWebhookPayload) -> tuple[str | None, str | None]:
    nested_issue = payload.data.get("issue")
    issue = nested_issue if isinstance(nested_issue, dict) else {}
    identifier = _optional_text(payload.data.get("identifier")) or _optional_text(
        issue.get("identifier")
    )
    title = _optional_text(payload.data.get("title")) or _optional_text(issue.get("title"))
    if identifier is None:
        match = _LINEAR_ISSUE_URL.search(payload.url)
        identifier = match.group(1) if match is not None else None
    return identifier, title


def _control_title(payload: _LinearWebhookPayload) -> str:
    identifier, title = _issue_display_fields(payload)
    if identifier is not None and title is not None:
        value = f"[{identifier}] {title}"
    elif identifier is not None:
        value = f"[{identifier}] Linear issue"
    elif title is not None:
        value = f"Linear | {title}"
    else:
        value = "Linear issue"
    return value[:_DISCORD_THREAD_TITLE_LIMIT]


def _issue_state_name(payload: _LinearWebhookPayload) -> str:
    nested_issue = payload.data.get("issue")
    issue = nested_issue if isinstance(nested_issue, dict) else {}
    for candidate in (payload.data.get("state"), issue.get("state")):
        if not isinstance(candidate, dict):
            continue
        name = _optional_text(candidate.get("name"))
        if name is not None:
            return name
    return ""


def _issue_control_closed(payload: _LinearWebhookPayload) -> bool | None:
    state_name = _issue_state_name(payload)
    return state_name == _DONE_STATE_NAME if state_name else None


def _issue_label(payload: _LinearWebhookPayload, issue_id: str) -> str:
    identifier, _title = _issue_display_fields(payload)
    return identifier or issue_id


def _comment_context(payload: _LinearWebhookPayload, issue_id: str) -> str:
    body = payload.data.get("body")
    comment = body if isinstance(body, str) and body else "(empty comment)"
    action = {
        "create": "posted",
        "update": "edited",
        "remove": "removed",
    }[payload.action]
    return (
        f"Issue: {_issue_label(payload, issue_id)}\n"
        f"Event: comment {action}\n"
        f"Author: {_actor_name(payload)}\n"
        f"Comment:\n{comment}"
    )


def _comment_instructions(payload: _LinearWebhookPayload) -> str:
    activity = {
        "create": "A new comment was posted",
        "update": "A comment was edited",
        "remove": "A comment was removed",
    }[payload.action]
    return (
        f"{activity} on the Linear issue bound to this thread. Read it and take appropriate "
        "action under the issue's current workflow state."
    )


def _issue_context(payload: _LinearWebhookPayload, issue_id: str) -> str:
    state_name = _issue_state_name(payload) or "unknown"
    updated_fields = ", ".join(sorted(payload.updated_from or {})) or "none reported"
    return (
        f"Issue: {_issue_label(payload, issue_id)}\n"
        f"Event: issue {payload.action}\n"
        f"State: {state_name}\n"
        f"Changed fields: {updated_fields}"
    )


def _conversation(payload: _LinearWebhookPayload, issue_id: str) -> WebhookConversation:
    return WebhookConversation(
        subject=ConversationSubject(
            namespace=ConversationSubjectNamespace(f"linear:{payload.organization_id}:issue"),
            key=ConversationSubjectKey(issue_id),
        ),
        control_title=_control_title(payload),
        control_closed=_issue_control_closed(payload),
    )


def _comment_event(
    payload: _LinearWebhookPayload,
    delivery_id: str,
) -> WebhookEvent:
    _required_data_text(payload, "id")
    issue_id = _required_data_text(payload, "issueId")
    return WebhookEvent(
        delivery_id=delivery_id,
        event_type=payload.type,
        action=payload.action,
        subject_id=issue_id,
        occurred_at=payload.created_at,
        instructions=_comment_instructions(payload),
        external_context=_comment_context(payload, issue_id),
        conversation=_conversation(payload, issue_id),
    )


def _issue_event(
    payload: _LinearWebhookPayload,
    delivery_id: str,
) -> WebhookEvent:
    issue_id = _required_data_text(payload, "id")
    return WebhookEvent(
        delivery_id=delivery_id,
        event_type=payload.type,
        action=payload.action,
        subject_id=issue_id,
        occurred_at=payload.created_at,
        instructions=_LINEAR_ISSUE_INSTRUCTIONS,
        external_context=_issue_context(payload, issue_id),
        conversation=_conversation(payload, issue_id),
    )


async def _event_workspace(event: WebhookEvent, config: LinearWebhookRouteConfig) -> str:
    async with linear_client(account_name=config.tool) as client:
        if config.workspace is not None:
            await workspace_issue(client, config.workspace, event.subject_id)
            return config.workspace
        issue = await client.get_issue(event.subject_id)
        project = issue.get("project") if issue is not None else None
        project_id = project.get("id") if isinstance(project, dict) else None
        workspace = (
            workspace_for_linear_project(project_id) if isinstance(project_id, str) else None
        )
        if workspace is None:
            raise LinearWorkspaceIssueError("Linear issue is not on a managed workspace board")
        return workspace


async def prepare_linear_webhook_event(
    event: WebhookEvent,
    *,
    config: LinearWebhookRouteConfig,
    public_source: bool = True,
) -> WebhookEvent:
    """Confirm workspace-board ownership before creating or waking an issue thread."""
    if event.instructions is None:
        return event
    try:
        workspace = await _event_workspace(event, config)
    except LinearWorkspaceIssueError:
        return replace(
            event,
            instructions=None,
            external_context=None,
            ignored_reason="issue_is_not_on_workspace_board",
            conversation=None,
        )
    except (aiohttp.ClientError, LinearBoardError, LinearError, TimeoutError, ValueError) as exc:
        raise WebhookProcessingError(str(exc)) from exc
    if event.conversation is None:
        return event
    return replace(
        event,
        conversation=replace(
            event.conversation,
            workspace=workspace,
            public_source=public_source,
        ),
    )


def parse_linear_webhook(
    raw_body: bytes,
    raw_headers: Mapping[str, str],
    secret: str,
    now: datetime,
    *,
    config: LinearWebhookRouteConfig,
) -> WebhookEvent:
    """Authenticate and parse one Linear delivery into a closed event contract."""
    delivery_id, timestamp = _authenticate(raw_body, raw_headers, secret, now, config)
    payload = _parse_payload(raw_body, timestamp)
    if config.organization_id and payload.organization_id != config.organization_id:
        raise WebhookPayloadError("Linear webhook organization does not match the route")
    if payload.type == "Comment":
        return _comment_event(payload, delivery_id)
    if payload.type == "Issue":
        return _issue_event(payload, delivery_id)
    subject_id = payload.data.get("id")
    return _ignored_event(
        payload,
        delivery_id,
        subject_id=subject_id if isinstance(subject_id, str) else payload.type,
        reason="event_type_is_not_configured",
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
                routes_conversations=True,
                candidate_workspaces=(
                    configured_linear_workspace_names(config.tool)
                    if config.workspace is None
                    else ()
                ),
                allow_admin_workspaces=config.workspace is None,
            )
        )
    return tuple(routes)
