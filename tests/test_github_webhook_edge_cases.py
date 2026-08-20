"""Edge-case coverage for GitHub webhook parsing and notification mapping."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from pynchy.plugins.api import WebhookAuthenticationError, WebhookDiscard, WebhookPayloadError
from pynchy.plugins.integrations.github_webhooks import (
    GITHUB_MAX_WEBHOOK_BODY_BYTES,
    GitHubWebhookRouteConfig,
    parse_github_webhook,
)

_SECRET = "github-webhook-edge-secret"  # noqa: S105  # pragma: allowlist secret
_REPOSITORY = "example/project"
_NOW = datetime(2026, 7, 29, tzinfo=UTC)


def _payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "opened",
        "number": 42,
        "repository": {"full_name": _REPOSITORY},
        "sender": {"login": "repo-owner"},
        "pull_request": {"number": 42},
    }
    payload.update(updates)
    return payload


def _request(payload: dict[str, object], event_type: str) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return raw_body, {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-GitHub-Delivery": "delivery-edge",
        "X-GitHub-Event": event_type,
    }


def _config() -> GitHubWebhookRouteConfig:
    return GitHubWebhookRouteConfig(name="project", repository=_REPOSITORY)


def _parse(payload: dict[str, object], event_type: str):
    raw_body, headers = _request(payload, event_type)
    return parse_github_webhook(raw_body, headers, _SECRET, _NOW, config=_config())


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"name": "   "}, "text fields cannot be empty"),
        ({"repository": "example"}, "owner/repository form"),
        ({"rate_limit_requests": 0}, "limits must be positive"),
        ({"max_body_bytes": GITHUB_MAX_WEBHOOK_BODY_BYTES + 1}, "cannot exceed"),
    ],
)
def test_route_configuration_rejects_invalid_limits_and_identifiers(
    updates: dict[str, object], message: str
) -> None:
    values = {"name": "project", "workspace": "project", "repository": _REPOSITORY}

    with pytest.raises(ValueError, match=message):
        GitHubWebhookRouteConfig(**(values | updates))


def test_route_configuration_accepts_a_smaller_payload_limit() -> None:
    config = GitHubWebhookRouteConfig(
        name="project",
        repository=_REPOSITORY,
        max_body_bytes=1024,
    )

    assert config.max_body_bytes == 1024


def test_sender_allowlist_rejects_blank_and_duplicate_logins() -> None:
    values = {"name": "project", "workspace": "project", "repository": _REPOSITORY}

    with pytest.raises(ValueError, match="cannot contain blank"):
        GitHubWebhookRouteConfig(**values, allowed_senders=("repo-owner", " "))
    with pytest.raises(ValueError, match="cannot contain duplicates"):
        GitHubWebhookRouteConfig(**values, allowed_senders=("repo-owner", "REPO-OWNER"))


def test_sender_allowlist_discards_untrusted_or_missing_senders() -> None:
    config = GitHubWebhookRouteConfig(
        name="project",
        repository=_REPOSITORY,
        allowed_senders=("repo-owner",),
    )
    untrusted_body, untrusted_headers = _request(
        _payload(sender={"login": "drive-by"}),
        "pull_request",
    )
    missing_body, missing_headers = _request(
        _payload(sender=None),
        "pull_request",
    )

    untrusted = parse_github_webhook(
        untrusted_body, untrusted_headers, _SECRET, _NOW, config=config
    )
    missing = parse_github_webhook(missing_body, missing_headers, _SECRET, _NOW, config=config)

    assert isinstance(untrusted, WebhookDiscard)
    assert isinstance(missing, WebhookDiscard)


def test_authentication_requires_all_provider_headers() -> None:
    raw_body, headers = _request(_payload(), "pull_request")

    for missing in headers:
        with pytest.raises(WebhookAuthenticationError, match="required"):
            parse_github_webhook(
                raw_body,
                {key: value for key, value in headers.items() if key != missing},
                _SECRET,
                _NOW,
                config=_config(),
            )


@pytest.mark.parametrize("blank_header", ["X-GitHub-Delivery", "X-GitHub-Event"])
def test_authentication_rejects_blank_delivery_metadata(blank_header: str) -> None:
    raw_body, headers = _request(_payload(), "pull_request")
    headers[blank_header] = "   "
    headers["X-Hub-Signature-256"] = (
        "sha256=" + hmac.new(_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    )

    with pytest.raises(WebhookPayloadError, match="cannot be blank"):
        parse_github_webhook(raw_body, headers, _SECRET, _NOW, config=_config())


def test_parser_rejects_non_json_and_schema_invalid_bodies() -> None:
    raw_body = b"not-json"
    signature = hmac.new(_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-GitHub-Delivery": "delivery-edge",
        "X-GitHub-Event": "pull_request",
    }
    with pytest.raises(WebhookPayloadError, match="not JSON"):
        parse_github_webhook(raw_body, headers, _SECRET, _NOW, config=_config())

    raw_body, headers = _request({"action": "opened"}, "pull_request")
    with pytest.raises(WebhookPayloadError, match="does not match"):
        parse_github_webhook(raw_body, headers, _SECRET, _NOW, config=_config())


def test_pull_request_events_cover_configured_notifications_and_ignored_actions() -> None:
    synchronize = _parse(_payload(action="synchronize"), "pull_request")
    assert "new commits pushed" in (synchronize.host_message or "")

    title = _parse(_payload(action="edited", changes={"title": {"from": "old"}}), "pull_request")
    assert "PR title updated" in (title.host_message or "")

    opened = _parse(_payload(action="opened"), "pull_request")
    assert "PR opened" in (opened.host_message or "")

    ignored = _parse(_payload(action="labeled"), "pull_request")
    assert ignored.ignored_reason == "pull_request_action_is_not_configured"


def test_pull_request_event_requires_a_pull_request_number() -> None:
    payload = _payload()
    payload.pop("number")
    payload["pull_request"] = {}

    with pytest.raises(WebhookPayloadError, match="no pull request number"):
        _parse(payload, "pull_request")


def test_issue_comment_events_distinguish_pr_comments_and_ignored_actions() -> None:
    non_pr = _payload(action="created")
    non_pr.pop("pull_request")
    non_pr["issue"] = {"number": 42}
    event = _parse(non_pr, "issue_comment")
    assert event.ignored_reason == "issue_comment_is_not_on_a_pull_request"

    pr_comment = _payload(action="created")
    pr_comment.pop("pull_request")
    pr_comment["issue"] = {"number": 42, "pull_request": {}}
    event = _parse(pr_comment, "issue_comment")
    assert event.instructions is not None
    assert event.external_context is not None

    ignored = dict(pr_comment, action="deleted")
    event = _parse(ignored, "issue_comment")
    assert event.ignored_reason == "issue_comment_action_is_not_configured"


def test_review_events_require_pull_requests_and_ignore_unknown_actions() -> None:
    missing = _payload(action="submitted")
    missing.pop("pull_request")
    missing["review"] = {"state": "commented"}
    with pytest.raises(WebhookPayloadError, match="review event has no"):
        _parse(missing, "pull_request_review")

    ignored = _parse(
        _payload(action="dismissed", review={"state": "commented"}),
        "pull_request_review",
    )
    assert ignored.host_message is not None

    unsupported = _parse(
        _payload(action="assigned", review={"state": "commented"}),
        "pull_request_review",
    )
    assert unsupported.ignored_reason == "pull_request_review_action_is_not_configured"


def test_review_comment_events_require_pull_requests_and_ignore_unknown_actions() -> None:
    missing = _payload(action="created")
    missing.pop("pull_request")
    with pytest.raises(WebhookPayloadError, match="review comment event has no"):
        _parse(missing, "pull_request_review_comment")

    unsupported = _parse(_payload(action="deleted"), "pull_request_review_comment")
    assert unsupported.ignored_reason == "pull_request_review_comment_action_is_not_configured"


def test_check_run_events_cover_ignored_and_multi_pull_request_failures() -> None:
    missing = _payload(action="completed")
    with pytest.raises(WebhookPayloadError, match="has no check run"):
        _parse(missing, "check_run")

    ignored = _parse(
        _payload(
            action="completed",
            check_run={"name": "tests", "conclusion": "success"},
        ),
        "check_run",
    )
    assert ignored.ignored_reason == "check_run_is_not_a_configured_failure"

    no_pr = _parse(
        _payload(
            action="completed",
            check_run={"name": "tests", "conclusion": "failure"},
        ),
        "check_run",
    )
    assert no_pr.ignored_reason == "check_run_is_not_associated_with_a_pull_request"

    multiple = _parse(
        _payload(
            action="completed",
            check_run={
                "name": "tests",
                "conclusion": "failure",
                "pull_requests": [{"number": 41}, {"number": 42}],
            },
        ),
        "check_run",
    )
    assert multiple.host_message is not None
    assert multiple.instructions is None


def test_unknown_event_type_is_ignored() -> None:
    event = _parse(_payload(), "workflow_run")

    assert event.ignored_reason == "event_type_is_not_configured"
