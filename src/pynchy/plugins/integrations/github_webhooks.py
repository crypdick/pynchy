"""Authenticated GitHub pull-request webhook routes and notification mapping."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import (  # noqa: TC003 - beartype resolves parser annotations at runtime.
    Mapping,
)
from dataclasses import dataclass
from datetime import (
    datetime,  # noqa: TC003 - beartype resolves parser annotations at runtime.
)
from functools import partial

from pydantic import ValidationError

from pynchy.plugins.api import (
    WebhookAuthenticationError,
    WebhookDiscard,
    WebhookEvent,
    WebhookPayloadError,
    WebhookRoute,
)
from pynchy.plugins.integrations.github_webhook_linear import (
    prepare_github_webhook_event,
)
from pynchy.plugins.integrations.github_webhook_models import (
    GITHUB_MAX_WEBHOOK_BODY_BYTES,
    GitHubEnvelope,
    GitHubWebhookRouteConfig,
)

__all__ = ["GITHUB_MAX_WEBHOOK_BODY_BYTES"]

_FAILURE_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "startup_failure", "timed_out"}
)
_REVIEW_INSTRUCTIONS = (
    "GitHub reports actionable pull-request review feedback on the PR attached to this "
    "Linear issue. Fetch the current unresolved review details with GitHub tools and triage "
    "them. Implement warranted changes in the existing worktree, run the repository's local "
    "CI, and update the same PR. Do not merge or deploy solely because of this webhook."
)
# GitHub check results supply evidence only. Local CI remains authoritative so
# automated follow-up does not spend hosted CI credits rerunning the same checks.
_CHECK_INSTRUCTIONS = (
    "GitHub reports a failed pull-request check on the PR attached to this Linear issue. "
    "Inspect the failure and determine whether the PR needs a change. If it does, implement "
    "the fix in the existing worktree and run the repository's local CI. Do not rerun GitHub "
    "CI, merge, or deploy solely because of this webhook."
)


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


def _payload(raw_body: bytes) -> GitHubEnvelope:
    try:
        decoded = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise WebhookPayloadError("GitHub webhook body is not JSON") from exc
    try:
        return GitHubEnvelope.model_validate(decoded)
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


def _actionable_pr_event(
    context: _DeliveryContext,
    *,
    action: str,
    number: int,
    instructions: str,
    details: Mapping[str, object],
) -> WebhookEvent:
    return WebhookEvent(
        delivery_id=context.delivery_id,
        event_type=context.event_type,
        action=action,
        subject_id=str(number),
        occurred_at=context.occurred_at,
        instructions=instructions,
        external_context={
            "repository": context.repository,
            "pull_request_number": number,
            "pull_request_url": _pr_url(context.repository, number),
            **details,
        },
    )


def _pull_request_event(
    payload: GitHubEnvelope,
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
    if payload.action == "closed":
        return _ignored_event(
            context,
            action=(
                "merged"
                if payload.pull_request is not None and payload.pull_request.merged
                else "closed"
            ),
            subject_id=str(number),
            reason="pull_request_closed",
        )
    if has_merge_conflict:
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


def _issue_comment_event(
    payload: GitHubEnvelope,
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
    message = f"GitHub PR update — {context.repository}#{issue.number}: {text}.\n{url}"
    return _actionable_pr_event(
        context,
        action=payload.action,
        number=issue.number,
        instructions=_REVIEW_INSTRUCTIONS,
        details={
            "event": "issue_comment",
            "fallback_host_message": message,
        },
    )


def _review_event(
    payload: GitHubEnvelope,
    context: _DeliveryContext,
) -> WebhookEvent:
    number = payload.pull_request_number()
    if payload.pull_request is None or number is None:
        raise WebhookPayloadError("GitHub pull request review event has no pull request number")
    review_state = (payload.review.state if payload.review is not None else None) or "updated"
    if payload.action not in {"submitted", "edited", "dismissed"}:
        return _ignored_event(
            context,
            action=payload.action,
            subject_id=str(number),
            reason="pull_request_review_action_is_not_configured",
        )
    message = (
        f"GitHub PR update — {context.repository}#{number}: review {payload.action} "
        f"({review_state.lower()}).\n{_pr_url(context.repository, number)}"
    )
    if payload.action == "dismissed" or review_state.casefold() == "approved":
        return _notification_event(
            context,
            action=payload.action,
            subject_id=str(number),
            message=message,
        )
    return _actionable_pr_event(
        context,
        action=payload.action,
        number=number,
        instructions=_REVIEW_INSTRUCTIONS,
        details={
            "event": "pull_request_review",
            "review_state": review_state,
            "fallback_host_message": message,
        },
    )


def _review_comment_event(
    payload: GitHubEnvelope,
    context: _DeliveryContext,
) -> WebhookEvent:
    number = payload.pull_request_number()
    if payload.pull_request is None or number is None:
        raise WebhookPayloadError("GitHub review comment event has no pull request number")
    if payload.action not in {"created", "edited"}:
        return _ignored_event(
            context,
            action=payload.action,
            subject_id=str(number),
            reason="pull_request_review_comment_action_is_not_configured",
        )
    text = (
        "new inline review comment"
        if payload.action == "created"
        else "inline review comment edited"
    )
    url = _pr_url(context.repository, number)
    message = f"GitHub PR update — {context.repository}#{number}: {text}.\n{url}"
    return _actionable_pr_event(
        context,
        action=payload.action,
        number=number,
        instructions=_REVIEW_INSTRUCTIONS,
        details={
            "event": "pull_request_review_comment",
            "fallback_host_message": message,
        },
    )


def _check_run_event(
    payload: GitHubEnvelope,
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
    message = (
        f"GitHub CI failure — {context.repository} {pr_label}: {check_run.name} "
        f"({conclusion.replace('_', ' ')}).\n{links}"
    )
    if len(pull_numbers) != 1:
        return _notification_event(
            context,
            action=payload.action,
            subject_id=subject_id,
            message=message,
        )
    return _actionable_pr_event(
        context,
        action=payload.action,
        number=pull_numbers[0],
        instructions=_CHECK_INSTRUCTIONS,
        details={
            "event": "check_run",
            "check_name": check_run.name,
            "conclusion": conclusion,
            "fallback_host_message": message,
        },
    )


def parse_github_webhook(  # noqa: PLR0911 - each supported event has one closed disposition.
    raw_body: bytes,
    raw_headers: Mapping[str, str],
    secret: str,
    now: datetime,
    *,
    config: GitHubWebhookRouteConfig,
) -> WebhookEvent | WebhookDiscard:
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
    sender = payload.sender.login if payload.sender is not None else None
    if config.allowed_senders and (
        sender is None
        or sender.casefold() not in {allowed.casefold() for allowed in config.allowed_senders}
    ):
        return WebhookDiscard()
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


def github_webhook_routes(
    configs: tuple[GitHubWebhookRouteConfig, ...],
) -> tuple[WebhookRoute, ...]:
    """Build explicitly mapped GitHub routes from resolved route configuration."""
    return tuple(
        WebhookRoute(
            provider="github",
            name=config.name,
            workspace=config.workspace,
            secret_env=config.secret_env,
            parse=partial(parse_github_webhook, config=config),
            max_body_bytes=config.max_body_bytes,
            rate_limit_requests=config.rate_limit_requests,
            rate_limit_window_seconds=config.rate_limit_window_seconds,
            prepare_event=partial(prepare_github_webhook_event, config=config),
            routes_conversations=True,
            public_source=not config.allowed_senders,
            allow_admin_workspaces=bool(config.allowed_senders),
        )
        for config in configs
    )
