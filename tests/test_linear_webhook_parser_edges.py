"""Public Linear webhook parser behavior for malformed and sparse payloads."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

import pytest
from linear_webhook_test_support import (
    SIGNING_KEY,
    payload,
    route_config,
    signed_request,
)

from pynchy.plugins.api import WebhookAuthenticationError, WebhookPayloadError
from pynchy.plugins.integrations.linear_webhooks import parse_linear_webhook

_NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _parse(body: dict[str, object]):
    raw_body, headers = signed_request(body)
    return parse_linear_webhook(raw_body, headers, SIGNING_KEY, _NOW, config=route_config())


def test_parser_rejects_invalid_delivery_id():
    body = payload(now=_NOW)
    raw_body, headers = signed_request(body)
    headers["Linear-Delivery"] = "not-a-uuid"

    with pytest.raises(WebhookAuthenticationError, match="delivery ID"):
        parse_linear_webhook(raw_body, headers, SIGNING_KEY, _NOW, config=route_config())


def test_parser_rejects_invalid_timestamp_header():
    body = payload(now=_NOW)
    raw_body, headers = signed_request(body)
    headers["Linear-Timestamp"] = "not-a-timestamp"

    with pytest.raises(WebhookAuthenticationError, match="timestamp"):
        parse_linear_webhook(raw_body, headers, SIGNING_KEY, _NOW, config=route_config())


def test_parser_rejects_malformed_json():
    raw_body = b"{"  # pragma: allowlist secret
    signature = hmac.new(SIGNING_KEY.encode(), raw_body, hashlib.sha256).hexdigest()
    headers = {
        "Linear-Signature": signature,
        "Linear-Delivery": "234d1a4e-b617-4388-90fe-adc3633d6b72",
        "Linear-Timestamp": str(int(_NOW.timestamp() * 1000)),
    }

    with pytest.raises(WebhookPayloadError, match="does not match its schema"):
        parse_linear_webhook(raw_body, headers, SIGNING_KEY, _NOW, config=route_config())


def test_parser_rejects_payload_header_timestamp_mismatch():
    body = payload(now=_NOW)
    raw_body, headers = signed_request(body)
    headers["Linear-Timestamp"] = str(int(_NOW.timestamp() * 1000) + 1)

    with pytest.raises(WebhookPayloadError, match="timestamps differ"):
        parse_linear_webhook(raw_body, headers, SIGNING_KEY, _NOW, config=route_config())


def test_parser_rejects_schema_missing_required_comment_id():
    body = payload(now=_NOW, data={"issueId": "issue-1", "body": "Review this"})

    with pytest.raises(WebhookPayloadError, match=r"missing data\.id"):
        _parse(body)


def test_parser_supports_nested_issue_id():
    event = _parse(
        payload(
            now=_NOW,
            event_type="Issue",
            action="update",
            data={
                "issue": {
                    "id": "issue-1",
                    "identifier": "PYN-1",
                    "title": "Nested issue",
                }
            },
        )
    )

    assert event.subject_id == "issue-1"
    assert event.conversation is not None
    assert event.conversation.control_title == "[PYN-1] Nested issue"


def test_parser_rejects_issue_without_direct_or_nested_id():
    body = payload(
        now=_NOW,
        event_type="Issue",
        action="update",
        data={"title": "Missing identity"},
    )

    with pytest.raises(WebhookPayloadError, match=r"missing data\.id"):
        _parse(body)


def test_parser_uses_unknown_actor_fallback_when_actor_is_absent():
    body = payload(now=_NOW)
    body["actor"] = None

    event = _parse(body)

    assert event.actor is None
    assert event.external_context is not None
    assert "Author: Unknown" in event.external_context


def test_parser_ignores_incomplete_actor_identity():
    body = payload(now=_NOW)
    body["actor"] = {"id": "", "type": "", "name": ""}

    event = _parse(body)

    assert event.actor is None


def test_parser_builds_title_only_control_title_without_url_identifier():
    event = _parse(
        payload(
            now=_NOW,
            event_type="Issue",
            action="update",
            url="",
            data={"id": "issue-1", "title": "A title"},
        )
    )

    assert event.conversation is not None
    assert event.conversation.control_title == "Linear | A title"


def test_parser_uses_generic_control_title_without_issue_display_fields():
    event = _parse(
        payload(
            now=_NOW,
            event_type="Issue",
            action="update",
            url="",
            data={"id": "issue-1"},
        )
    )

    assert event.conversation is not None
    assert event.conversation.control_title == "Linear issue"


def test_parser_handles_issue_without_state_evidence():
    event = _parse(
        payload(
            now=_NOW,
            event_type="Issue",
            action="update",
            data={"id": "issue-1", "state": {}},
        )
    )

    assert event.lifecycle is None
    assert event.conversation is not None


def test_parser_rejects_route_organization_mismatch():
    body = payload(now=_NOW)
    body["organizationId"] = "other-org"

    with pytest.raises(WebhookPayloadError, match="organization"):
        _parse(body)
