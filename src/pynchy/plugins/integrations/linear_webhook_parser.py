"""Authenticated Linear payload parsing and closed event mapping."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import (
    Mapping,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from pynchy.conversation.api import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.plugins.api import (
    WebhookActor,
    WebhookAuthenticationError,
    WebhookConversation,
    WebhookEvent,
    WebhookLifecycle,
    WebhookPayloadError,
)
from pynchy.plugins.integrations.linear_board_payloads import norm_name
from pynchy.plugins.integrations.linear_statuses import (
    AGENT_PROPOSED_STATUS,
    LINEAR_TODO_STATUSES,
    TERMINAL_STATE_TYPES,
)
from pynchy.plugins.integrations.linear_webhook_config import (
    LinearWebhookRouteConfig,
)
from pynchy.plugins.integrations.linear_webhook_evidence import (
    comment_webhook_evidence,
    issue_state_webhook_evidence,
)
from pynchy.plugins.integrations.linear_webhook_prompts import (
    LinearWebhookPrompts,
)
from pynchy.webhook_effects import (
    WebhookEffectEvidence,
)

# NOTE: Update docs/integrations/linear.md "Receive Linear callbacks" and
# docs/architecture/conversation-routing.md "Linear Issue Webhooks" if this
# event-admission or prompt contract changes.
_LINEAR_ISSUE_URL = re.compile(r"/issue/([^/#?]+)", re.IGNORECASE)
_DISCORD_THREAD_TITLE_LIMIT = 100


class _LinearActor(BaseModel):
    model_config = {"extra": "ignore"}

    id: str = ""
    type: str = ""
    name: str = ""


@dataclass(frozen=True)
class _LinearIssueState:
    """Provider state evidence preserved from one Issue callback."""

    id: str | None
    name: str | None
    state_type: str | None


class _LinearWebhookPayload(BaseModel):
    model_config = {"extra": "ignore", "populate_by_name": True}

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


def _issue_id(payload: _LinearWebhookPayload) -> str:
    """Read the immutable issue key from either documented Issue payload shape."""
    direct = _optional_text(payload.data.get("id"))
    if direct is not None:
        return direct
    nested_issue = payload.data.get("issue")
    issue = nested_issue if isinstance(nested_issue, dict) else {}
    nested = _optional_text(issue.get("id"))
    if nested is not None:
        return nested
    raise WebhookPayloadError(f"Linear {payload.type} payload is missing data.id")


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


def _actor(payload: _LinearWebhookPayload) -> WebhookActor | None:
    if payload.actor is None:
        return None
    actor_id = payload.actor.id.strip()
    actor_kind = payload.actor.type.strip()
    if not actor_id or not actor_kind:
        return None
    return WebhookActor(id=actor_id, kind=actor_kind)


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


def _issue_state(payload: _LinearWebhookPayload) -> _LinearIssueState | None:
    nested_issue = payload.data.get("issue")
    issue = nested_issue if isinstance(nested_issue, dict) else {}
    for candidate in (payload.data.get("state"), issue.get("state")):
        if not isinstance(candidate, dict):
            continue
        state = _LinearIssueState(
            id=_optional_text(candidate.get("id")),
            name=_optional_text(candidate.get("name")),
            state_type=_optional_text(candidate.get("type")),
        )
        if state.id is not None or state.name is not None or state.state_type is not None:
            return state
    return None


def _is_agent_proposed_issue_creation(
    payload: _LinearWebhookPayload,
    state: _LinearIssueState | None,
) -> bool:
    return (
        payload.action == "create"
        and state is not None
        and norm_name(state.name) == norm_name(LINEAR_TODO_STATUSES[AGENT_PROPOSED_STATUS].name)
    )


def _issue_state_name(payload: _LinearWebhookPayload) -> str:
    state = _issue_state(payload)
    return state.name if state is not None and state.name is not None else ""


def _issue_control_closed(payload: _LinearWebhookPayload) -> bool | None:
    state = _issue_state(payload)
    if state is None or state.state_type is None:
        return None
    return state.state_type in TERMINAL_STATE_TYPES


def _issue_control_state_revision(payload: _LinearWebhookPayload) -> str | None:
    """Return the provider revision that orders one typed issue-state observation."""
    nested_issue = payload.data.get("issue")
    issue = nested_issue if isinstance(nested_issue, dict) else {}
    return _optional_text(payload.data.get("updatedAt")) or _optional_text(issue.get("updatedAt"))


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


def _comment_effect_evidence(
    payload: _LinearWebhookPayload,
    config: LinearWebhookRouteConfig,
) -> WebhookEffectEvidence | None:
    """Build an exact selector only from provider-side Comment/create evidence."""
    if payload.action != "create":
        return None
    comment_id = _optional_text(payload.data.get("id"))
    issue_id = _optional_text(payload.data.get("issueId"))
    revision = _optional_text(payload.data.get("updatedAt"))
    if comment_id is None or issue_id is None or revision is None:
        return None
    return comment_webhook_evidence(
        config.tool,
        comment_id=comment_id,
        issue_id=issue_id,
        revision=revision,
    )


def _issue_state_effect_evidence(
    payload: _LinearWebhookPayload,
    config: LinearWebhookRouteConfig,
    *,
    issue_id: str,
    state: _LinearIssueState | None,
) -> WebhookEffectEvidence | None:
    """Build a selector only for exact nonterminal Issue/update evidence."""
    if (
        payload.action != "update"
        or state is None
        or state.id is None
        or state.state_type is None
        or state.state_type in TERMINAL_STATE_TYPES
    ):
        return None
    revision = _optional_text(payload.data.get("updatedAt"))
    if revision is None:
        return None
    return issue_state_webhook_evidence(
        config.tool,
        issue_id=issue_id,
        state_id=state.id,
        revision=revision,
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


def _conversation(
    payload: _LinearWebhookPayload,
    issue_id: str,
    *,
    control_closed: bool | None = None,
    control_state_revision: str | None = None,
) -> WebhookConversation:
    return WebhookConversation(
        subject=ConversationSubject(
            namespace=ConversationSubjectNamespace(f"linear:{payload.organization_id}:issue"),
            key=ConversationSubjectKey(issue_id),
        ),
        control_title=_control_title(payload),
        control_closed=control_closed,
        control_state_revision=control_state_revision,
    )


def _comment_event(
    payload: _LinearWebhookPayload,
    delivery_id: str,
    *,
    config: LinearWebhookRouteConfig,
    prompts: LinearWebhookPrompts,
) -> WebhookEvent:
    _required_data_text(payload, "id")
    issue_id = _required_data_text(payload, "issueId")
    return WebhookEvent(
        delivery_id=delivery_id,
        event_type=payload.type,
        action=payload.action,
        subject_id=issue_id,
        occurred_at=payload.created_at,
        instructions=prompts.comment,
        external_context=_comment_context(payload, issue_id),
        conversation=_conversation(payload, issue_id),
        actor=_actor(payload),
        changed_fields=frozenset(payload.updated_from or ()),
        effect_evidence=_comment_effect_evidence(payload, config),
    )


def _issue_event(
    payload: _LinearWebhookPayload,
    delivery_id: str,
    *,
    config: LinearWebhookRouteConfig,
    prompts: LinearWebhookPrompts,
) -> WebhookEvent:
    issue_id = _issue_id(payload)
    state = _issue_state(payload)
    # The signed callback state is immutable; later Linear transitions are separate deliveries.
    if _is_agent_proposed_issue_creation(payload, state):
        return _ignored_event(
            payload,
            delivery_id,
            subject_id=issue_id,
            reason="issue_creation_does_not_authorize_work",
        )
    control_closed = _issue_control_closed(payload)
    control_state_revision = _issue_control_state_revision(payload)
    conversation = _conversation(
        payload,
        issue_id,
        control_closed=control_closed,
        control_state_revision=control_state_revision,
    )
    if control_closed is True:
        return WebhookEvent(
            delivery_id=delivery_id,
            event_type=payload.type,
            action=payload.action,
            subject_id=issue_id,
            occurred_at=payload.created_at,
            instructions=None,
            external_context=None,
            conversation=conversation,
            lifecycle=WebhookLifecycle(
                context={"linear_state_id": state.id} if state is not None and state.id else None
            ),
            actor=_actor(payload),
            changed_fields=frozenset(payload.updated_from or ()),
        )
    return WebhookEvent(
        delivery_id=delivery_id,
        event_type=payload.type,
        action=payload.action,
        subject_id=issue_id,
        occurred_at=payload.created_at,
        instructions=prompts.issue,
        external_context=_issue_context(payload, issue_id),
        conversation=conversation,
        actor=_actor(payload),
        changed_fields=frozenset(payload.updated_from or ()),
        effect_evidence=_issue_state_effect_evidence(
            payload,
            config,
            issue_id=issue_id,
            state=state,
        ),
    )


# allow: too-many-arguments - authenticated parser keeps transport, route, and prompts explicit.
def parse_linear_webhook(  # noqa: PLR0913
    raw_body: bytes,
    raw_headers: Mapping[str, str],
    secret: str,
    now: datetime,
    *,
    config: LinearWebhookRouteConfig,
    prompts: LinearWebhookPrompts,
) -> WebhookEvent:
    """Authenticate and parse one Linear delivery into a closed event contract."""
    delivery_id, timestamp = _authenticate(raw_body, raw_headers, secret, now, config)
    payload = _parse_payload(raw_body, timestamp)
    if config.organization_id and payload.organization_id != config.organization_id:
        raise WebhookPayloadError("Linear webhook organization does not match the route")
    if payload.type == "Comment":
        return _comment_event(payload, delivery_id, config=config, prompts=prompts)
    if payload.type == "Issue":
        return _issue_event(payload, delivery_id, config=config, prompts=prompts)
    subject_id = payload.data.get("id")
    return _ignored_event(
        payload,
        delivery_id,
        subject_id=subject_id if isinstance(subject_id, str) else payload.type,
        reason="event_type_is_not_configured",
    )
