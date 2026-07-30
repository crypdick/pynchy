"""Public validation behavior for deferred webhook event payloads."""

from __future__ import annotations

import pytest

from pynchy.host.orchestrator.webhook_event_payloads import webhook_event_from_payload


def _payload() -> dict[str, object]:
    return {
        "delivery_id": "delivery-1",
        "event_type": "issue",
        "action": "updated",
        "subject_id": "issue-1",
        "occurred_at": "2026-07-29T12:00:00Z",
        "changed_fields": [],
    }


def _conversation() -> dict[str, object]:
    return {
        "subject_namespace": "linear",
        "subject_key": "issue-1",
        "control_title": "Issue 1",
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("conversation", "not an object", "conversation is not an object"),
        ("ignored_reason", 42, "ignored_reason is not a string"),
        ("changed_fields", ["title", 42], "changed_fields is not a string list"),
        ("external_context", 42, "external_context has an invalid shape"),
    ],
)
def test_event_payload_rejects_invalid_top_level_shapes(
    field: str, value: object, message: str
) -> None:
    payload = _payload()
    payload[field] = value

    with pytest.raises(TypeError, match=message):
        webhook_event_from_payload(payload)


def test_event_payload_rejects_missing_required_text() -> None:
    payload = _payload()
    del payload["delivery_id"]

    with pytest.raises(TypeError, match="delivery_id is not a non-empty string"):
        webhook_event_from_payload(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("control_closed", 1, "control_closed is not boolean"),
        ("control_state_revision", 1, "control_state_revision is not a string"),
        ("public_source", 1, "public_source is not boolean"),
    ],
)
def test_event_payload_rejects_invalid_conversation_fields(
    field: str, value: object, message: str
) -> None:
    conversation = _conversation()
    conversation[field] = value
    payload = _payload()
    payload["conversation"] = conversation

    with pytest.raises(TypeError, match=message):
        webhook_event_from_payload(payload)
