"""Validation coverage for provider-neutral webhook effect contracts."""

from __future__ import annotations

import pytest

from pynchy.webhook_effects import WebhookEffectEvidence, WebhookEffectScope


def _scope(**updates: object) -> WebhookEffectScope:
    values: dict[str, object] = {
        "provider": "github",
        "account": "pynchy",
        "event_type": "pull_request",
        "event_action": "opened",
        "subject_id": "42",
    }
    values.update(updates)
    return WebhookEffectScope(**values)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("provider", "provider"),
        ("account", "account"),
        ("event_type", "event type"),
        ("event_action", "event action"),
    ],
)
def test_scope_rejects_blank_identity_fields(field: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _scope(**{field: "  "})


def test_evidence_requires_a_subject_and_nonempty_fingerprint() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        WebhookEffectEvidence(scope=_scope(), fingerprint=" ")

    with pytest.raises(ValueError, match="requires a subject"):
        WebhookEffectEvidence(scope=_scope(subject_id=None), fingerprint="fingerprint")
