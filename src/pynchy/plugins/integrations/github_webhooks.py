"""Authenticated GitHub pull-request webhook routes and notification mapping."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves parser annotations at runtime.
    Mapping,
)
from dataclasses import dataclass
from datetime import (
    datetime,  # noqa: TC003, RUF100 - beartype resolves parser annotations at runtime.
)
from functools import partial

from pydantic import BaseModel, Field, ValidationError, field_validator

from pynchy.config import get_settings
from pynchy.plugins.integrations.github_pull_requests import GitHubPullRequestRef
from pynchy.plugins.integrations.linear_board_payloads import LinearBoardPayloadError
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_work_item_completion import complete_merged_pull_request
from pynchy.plugins.webhooks import (
    WebhookAuthenticationError,
    WebhookEvent,
    WebhookPayloadError,
    WebhookProcessingError,
    WebhookRoute,
)

# GitHub rejects webhook deliveries larger than this documented maximum, so this
# route accepts every payload GitHub can actually deliver without making ingress
# unbounded. Keep docs/integrations/github.md in sync with this provider contract.
GITHUB_MAX_WEBHOOK_BODY_BYTES = 25 * 1024 * 1024
_FAILURE_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "startup_failure", "timed_out"}
)


class _GitHubModel(BaseModel):
    model_config = {"extra": "ignore"}


class GitHubWebhookRouteConfig(_GitHubModel):
    """Plugin-owned configuration for one repository-to-workspace route."""

    model_config = {"extra": "forbid"}

    name: str
    workspace: str
    repository: str
    secret_env: str = "GITHUB_WEBHOOK_SECRET"  # noqa: S105, RUF100 - environment variable name, not a credential.
    max_body_bytes: int = GITHUB_MAX_WEBHOOK_BODY_BYTES
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    @field_validator("name", "workspace", "repository", "secret_env")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("GitHub webhook route text fields cannot be empty")
        return value

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if value.count("/") != 1 or any(not component for component in value.split("/")):
            raise ValueError("GitHub webhook repository must have owner/repository form")
        return value

    @field_validator("max_body_bytes", "rate_limit_requests", "rate_limit_window_seconds")
    @classmethod
    def validate_positive_limits(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("GitHub webhook limits must be positive")
        return value

    @field_validator("max_body_bytes")
    @classmethod
    def validate_github_payload_limit(cls, value: int) -> int:
        if value > GITHUB_MAX_WEBHOOK_BODY_BYTES:
            raise ValueError("GitHub webhook body limit cannot exceed GitHub's 25 MiB payload cap")
        return value


class GitHubPluginOptions(_GitHubModel):
    """Typed transport parser for ``[plugins.github.options]``."""

    model_config = {"extra": "forbid"}

    webhook_routes: tuple[GitHubWebhookRouteConfig, ...] = ()


class _GitHubRepository(_GitHubModel):
    full_name: str


class _GitHubPullRequest(_GitHubModel):
    html_url: str | None = None
    mergeable: bool | None = None
    mergeable_state: str | None = None
    merged: bool = False
    updated_at: str | None = None


class _GitHubIssue(_GitHubModel):
    number: int
    pull_request: dict[str, object] | None = None
    updated_at: str | None = None


class _GitHubReview(_GitHubModel):
    state: str | None = None
    submitted_at: str | None = None


class _GitHubCheckPullRequest(_GitHubModel):
    number: int


class _GitHubCheckRun(_GitHubModel):
    name: str
    conclusion: str | None = None
    pull_requests: tuple[_GitHubCheckPullRequest, ...] = ()
    completed_at: str | None = None


class _GitHubEnvelope(_GitHubModel):
    repository: _GitHubRepository
    action: str = ""
    number: int | None = None
    pull_request: _GitHubPullRequest | None = None
    issue: _GitHubIssue | None = None
    review: _GitHubReview | None = None
    check_run: _GitHubCheckRun | None = None
    changes: dict[str, object] = Field(default_factory=dict)


@dataclass(frozen=True)
class _DeliveryContext:
    """Route-bound metadata shared by one closed GitHub event mapping."""

    delivery_id: str
    event_type: str
    repository: str
    occurred_at: str


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Return one HTTP header without relying on a transport's casing rules."""
    normalized = name.lower()
    return next((value for key, value in headers.items() if key.lower() == normalized), None)


def _authenticate(raw_body: bytes, headers: Mapping[str, str], secret: str) -> tuple[str, str]:
    signature = _header(headers, "X-Hub-Signature-256")
    delivery_id = _header(headers, "X-GitHub-Delivery")
    event_type = _header(headers, "X-GitHub-Event")
    if signature is None or delivery_id is None or event_type is None:
        raise WebhookAuthenticationError("GitHub signature, delivery ID, and event are required")
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise WebhookAuthenticationError("GitHub webhook signature does not match")
    if not delivery_id.strip() or not event_type.strip():
        raise WebhookPayloadError("GitHub delivery ID and event cannot be blank")
    return delivery_id, event_type


def _payload(raw_body: bytes) -> _GitHubEnvelope:
    try:
        decoded = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise WebhookPayloadError("GitHub webhook body is not JSON") from exc
    try:
        return _GitHubEnvelope.model_validate(decoded)
    except ValidationError as exc:
        raise WebhookPayloadError("GitHub webhook payload does not match its schema") from exc


def _pr_url(repository: str, number: int) -> str:
    return f"https://github.com/{repository}/pull/{number}"


def _ignored_event(
    context: _DeliveryContext,
    *,
    action: str,
    subject_id: str,
    reason: str,
) -> WebhookEvent:
    return WebhookEvent(
        delivery_id=context.delivery_id,
        event_type=context.event_type,
        action=action,
        subject_id=subject_id,
        occurred_at=context.occurred_at,
        instructions=None,
        external_context=None,
        ignored_reason=reason,
    )


def _notification_event(
    context: _DeliveryContext,
    *,
    action: str,
    subject_id: str,
    message: str,
) -> WebhookEvent:
    return WebhookEvent(
        delivery_id=context.delivery_id,
        event_type=context.event_type,
        action=action,
        subject_id=subject_id,
        occurred_at=context.occurred_at,
        instructions=None,
        external_context=None,
        host_message=message,
    )


def _pull_request_event(
    payload: _GitHubEnvelope,
    context: _DeliveryContext,
) -> WebhookEvent:
    if payload.number is None:
        raise WebhookPayloadError("GitHub pull request event has no pull request number")
    number = payload.number
    url = _pr_url(context.repository, number)
    has_merge_conflict = payload.pull_request is not None and (
        payload.pull_request.mergeable is False
        or (payload.pull_request.mergeable_state or "").casefold() == "dirty"
    )
    event_action = payload.action
    if (
        payload.action == "closed"
        and payload.pull_request is not None
        and payload.pull_request.merged
    ):
        text = "PR merged"
        event_action = "merged"
    elif has_merge_conflict:
        text = "merge conflict detected"
    elif payload.action == "synchronize":
        text = "new commits pushed"
    elif payload.action == "edited" and "body" in payload.changes:
        text = "PR description updated"
    elif payload.action == "edited" and "title" in payload.changes:
        text = "PR title updated"
    elif payload.action in {
        "opened",
        "reopened",
        "closed",
        "ready_for_review",
        "converted_to_draft",
    }:
        text = f"PR {payload.action.replace('_', ' ')}"
    else:
        return _ignored_event(
            context,
            action=payload.action,
            subject_id=str(number),
            reason="pull_request_action_is_not_configured",
        )
    return _notification_event(
        context,
        action=event_action,
        subject_id=str(number),
        message=f"GitHub PR update — {context.repository}#{number}: {text}.\n{url}",
    )


async def _process_github_event(
    event: WebhookEvent,
    *,
    config: GitHubWebhookRouteConfig,
) -> None:
    """Apply trusted merge lifecycle effects before admitting the delivery receipt."""
    if event.event_type != "pull_request" or event.action != "merged":
        return
    try:
        pull_request = GitHubPullRequestRef.from_repository_number(
            config.repository,
            int(event.subject_id),
        )
        await complete_merged_pull_request(config.workspace, pull_request, event.delivery_id)
    except (LinearBoardPayloadError, LinearError, ValueError) as exc:
        raise WebhookProcessingError(str(exc)) from exc


def _issue_comment_event(
    payload: _GitHubEnvelope,
    context: _DeliveryContext,
) -> WebhookEvent:
    issue = payload.issue
    if issue is None or issue.pull_request is None:
        return _ignored_event(
            context,
            action=payload.action,
            subject_id=str(issue.number) if issue is not None else "issue",
            reason="issue_comment_is_not_on_a_pull_request",
        )
    if payload.action not in {"created", "edited"}:
        return _ignored_event(
            context,
            action=payload.action,
            subject_id=str(issue.number),
            reason="issue_comment_action_is_not_configured",
        )
    text = "new PR comment" if payload.action == "created" else "PR comment edited"
    url = _pr_url(context.repository, issue.number)
    return _notification_event(
        context,
        action=payload.action,
        subject_id=str(issue.number),
        message=f"GitHub PR update — {context.repository}#{issue.number}: {text}.\n{url}",
    )


def _review_event(
    payload: _GitHubEnvelope,
    context: _DeliveryContext,
) -> WebhookEvent:
    if payload.pull_request is None or payload.number is None:
        raise WebhookPayloadError("GitHub pull request review event has no pull request number")
    review_state = (payload.review.state if payload.review is not None else None) or "updated"
    if payload.action not in {"submitted", "edited", "dismissed"}:
        return _ignored_event(
            context,
            action=payload.action,
            subject_id=str(payload.number),
            reason="pull_request_review_action_is_not_configured",
        )
    return _notification_event(
        context,
        action=payload.action,
        subject_id=str(payload.number),
        message=(
            f"GitHub PR update — {context.repository}#{payload.number}: review {payload.action} "
            f"({review_state.lower()}).\n{_pr_url(context.repository, payload.number)}"
        ),
    )


def _review_comment_event(
    payload: _GitHubEnvelope,
    context: _DeliveryContext,
) -> WebhookEvent:
    if payload.pull_request is None or payload.number is None:
        raise WebhookPayloadError("GitHub review comment event has no pull request number")
    if payload.action not in {"created", "edited"}:
        return _ignored_event(
            context,
            action=payload.action,
            subject_id=str(payload.number),
            reason="pull_request_review_comment_action_is_not_configured",
        )
    text = (
        "new inline review comment"
        if payload.action == "created"
        else "inline review comment edited"
    )
    url = _pr_url(context.repository, payload.number)
    return _notification_event(
        context,
        action=payload.action,
        subject_id=str(payload.number),
        message=f"GitHub PR update — {context.repository}#{payload.number}: {text}.\n{url}",
    )


def _check_run_event(
    payload: _GitHubEnvelope,
    context: _DeliveryContext,
) -> WebhookEvent:
    check_run = payload.check_run
    if check_run is None:
        raise WebhookPayloadError("GitHub check run event has no check run")
    conclusion = (check_run.conclusion or "").lower()
    pull_numbers = tuple(pr.number for pr in check_run.pull_requests)
    subject_id = ",".join(str(number) for number in pull_numbers) or check_run.name
    if payload.action != "completed" or conclusion not in _FAILURE_CONCLUSIONS:
        return _ignored_event(
            context,
            action=payload.action,
            subject_id=subject_id,
            reason="check_run_is_not_a_configured_failure",
        )
    if not pull_numbers:
        return _ignored_event(
            context,
            action=payload.action,
            subject_id=subject_id,
            reason="check_run_is_not_associated_with_a_pull_request",
        )
    pr_label = ", ".join(f"#{number}" for number in pull_numbers)
    links = "\n".join(_pr_url(context.repository, number) for number in pull_numbers)
    return _notification_event(
        context,
        action=payload.action,
        subject_id=subject_id,
        message=(
            f"GitHub CI failure — {context.repository} {pr_label}: {check_run.name} "
            f"({conclusion.replace('_', ' ')}).\n{links}"
        ),
    )


def parse_github_webhook(
    raw_body: bytes,
    raw_headers: Mapping[str, str],
    secret: str,
    now: datetime,
    *,
    config: GitHubWebhookRouteConfig,
) -> WebhookEvent:
    """Authenticate and turn one GitHub delivery into a closed host notification."""
    delivery_id, event_type = _authenticate(raw_body, raw_headers, secret)
    payload = _payload(raw_body)
    if payload.repository.full_name.casefold() != config.repository.casefold():
        raise WebhookPayloadError("GitHub webhook repository does not match the route")
    context = _DeliveryContext(
        delivery_id=delivery_id,
        event_type=event_type,
        repository=config.repository,
        occurred_at=now.isoformat(),
    )
    if event_type == "pull_request":
        return _pull_request_event(payload, context)
    if event_type == "issue_comment":
        return _issue_comment_event(payload, context)
    if event_type == "pull_request_review":
        return _review_event(payload, context)
    if event_type == "pull_request_review_comment":
        return _review_comment_event(payload, context)
    if event_type == "check_run":
        return _check_run_event(payload, context)
    return _ignored_event(
        context,
        action=payload.action,
        subject_id=event_type,
        reason="event_type_is_not_configured",
    )


def github_webhook_routes() -> tuple[WebhookRoute, ...]:
    """Parse plugin options and return explicitly mapped GitHub routes."""
    plugin = get_settings().plugins.get("github")
    options = GitHubPluginOptions.model_validate(plugin.options if plugin is not None else {})
    return tuple(
        WebhookRoute(
            provider="github",
            name=config.name,
            workspace=config.workspace,
            secret_env=config.secret_env,
            parse=partial(parse_github_webhook, config=config),
            process_event=partial(_process_github_event, config=config),
            max_body_bytes=config.max_body_bytes,
            rate_limit_requests=config.rate_limit_requests,
            rate_limit_window_seconds=config.rate_limit_window_seconds,
        )
        for config in options.webhook_routes
    )
