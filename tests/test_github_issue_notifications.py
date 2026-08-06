"""Behavioral coverage for native GitHub Issue webhook notifications."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from pynchy.plugins.api import WebhookPayloadError
from pynchy.plugins.integrations.github_webhook_models import GitHubWebhookRouteConfig
from pynchy.plugins.integrations.github_webhooks import parse_github_webhook

_SIGNING_KEY = "github-webhook-test-signing-key-long-enough"
_REPOSITORY = "example/project"


def _config() -> GitHubWebhookRouteConfig:
    return GitHubWebhookRouteConfig(name="project", workspace="project", repository=_REPOSITORY)


def _issue_payload(
    *,
    action: str = "opened",
    changes: dict[str, object] | None = None,
    pull_request: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "action": action,
        "repository": {"full_name": _REPOSITORY},
        "issue": {
            "number": 42,
            "pull_request": pull_request,
            "title": "Provider prose must not reach the workspace",
            "body": "Neither must this body.",
        },
        "changes": changes or {},
    }


def _signed_request(payload: dict[str, object], event_type: str) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(_SIGNING_KEY.encode(), raw_body, hashlib.sha256).hexdigest()
    return raw_body, {
        "X-GitHub-Delivery": "ee7b4ec5-daa1-48fa-8c8f-c4de20e9d65f",
        "X-GitHub-Event": event_type,
        "X-Hub-Signature-256": f"sha256={signature}",
    }


@pytest.mark.parametrize(
    ("action", "changes", "text"),
    [
        ("opened", {}, "issue opened"),
        ("reopened", {}, "issue reopened"),
        ("closed", {}, "issue closed"),
        ("edited", {"title": {"from": "old"}}, "issue title updated"),
        ("edited", {"body": {"from": "old"}}, "issue description updated"),
    ],
)
def test_native_issue_event_maps_to_literal_notification(
    action: str, changes: dict[str, object], text: str
) -> None:
    raw_body, headers = _signed_request(_issue_payload(action=action, changes=changes), "issues")

    event = parse_github_webhook(
        raw_body, headers, _SIGNING_KEY, datetime.now(UTC), config=_config()
    )

    assert event.instructions is None
    assert event.external_context is None
    assert event.host_message == (
        f"GitHub issue update — example/project#42: {text}.\n"
        "https://github.com/example/project/issues/42"
    )


def test_native_issue_comments_are_literal_notifications() -> None:
    raw_body, headers = _signed_request(_issue_payload(action="created"), "issue_comment")

    event = parse_github_webhook(
        raw_body, headers, _SIGNING_KEY, datetime.now(UTC), config=_config()
    )

    assert event.host_message == (
        "GitHub issue update — example/project#42: new issue comment.\n"
        "https://github.com/example/project/issues/42"
    )


def test_native_issue_rejects_pr_backed_unsupported_and_invalid_events() -> None:
    raw_body, headers = _signed_request(
        _issue_payload(
            pull_request={"url": "https://api.github.com/repos/example/project/pulls/42"}
        ),
        "issues",
    )
    pr_backed = parse_github_webhook(
        raw_body, headers, _SIGNING_KEY, datetime.now(UTC), config=_config()
    )
    raw_body, headers = _signed_request(_issue_payload(action="labeled"), "issues")
    unsupported = parse_github_webhook(
        raw_body, headers, _SIGNING_KEY, datetime.now(UTC), config=_config()
    )
    invalid = _issue_payload()
    issue = invalid["issue"]
    assert isinstance(issue, dict)
    issue["number"] = 0
    raw_body, headers = _signed_request(invalid, "issues")

    assert pr_backed.ignored_reason == "issues_event_is_a_pull_request"
    assert unsupported.ignored_reason == "issues_action_is_not_configured"
    with pytest.raises(WebhookPayloadError, match="payload does not match"):
        parse_github_webhook(raw_body, headers, _SIGNING_KEY, datetime.now(UTC), config=_config())
