"""Behavior tests for request-local reversible LLM redaction."""

from __future__ import annotations

import json

import pytest

from pynchy.plugins.api import CapabilityId
from pynchy.redaction import (
    GatewayRedactionPosture,
    RedactionRequestError,
    RedactionSession,
    RestorationCapability,
    RestorationDeniedError,
    SensitiveDataClass,
    SinkExposure,
    detect_sensitive_spans,
    irreversibly_redact_llm_request_body,
    redact_llm_request_body,
    redaction_posture_for_gateway_mode,
)


def _credential() -> str:
    return "".join(("ghp_", "a" * 36))


def _private_capability(*classes: SensitiveDataClass) -> RestorationCapability:
    return RestorationCapability(
        capability_id=CapabilityId("test.private.restore"),
        exposure=SinkExposure.NON_PUBLIC,
        allowed_data_classes=frozenset(classes),
    )


def test_overlapping_patterns_select_secret_class_without_echoing_value() -> None:
    source = "password=user@example.test"

    spans = detect_sensitive_spans(source)

    assert len(spans) == 1
    assert spans[0].data_class is SensitiveDataClass.CREDENTIAL
    assert "user@example.test" not in repr(spans)


def test_gateway_mode_requires_an_explicit_redaction_posture() -> None:
    assert redaction_posture_for_gateway_mode("builtin") is GatewayRedactionPosture.ENFORCED
    assert redaction_posture_for_gateway_mode("litellm") is GatewayRedactionPosture.NOT_ENFORCED

    with pytest.raises(ValueError, match="Unknown gateway mode"):
        redaction_posture_for_gateway_mode("other")


def test_payment_card_redaction_requires_a_valid_luhn_checksum() -> None:
    valid = "4111 1111 1111 1111"
    valid_with_digit_doubling = "5555 5555 5555 4444"
    invalid = "4111 1111 1111 1112"
    all_identical = "1111 1111 1111 1111"

    spans = detect_sensitive_spans(
        f"valid={valid} valid2={valid_with_digit_doubling} invalid={invalid} same={all_identical}"
    )

    assert len(spans) == 2
    assert all(span.data_class is SensitiveDataClass.PAYMENT_CARD for span in spans)


def test_placeholder_injection_is_never_treated_as_a_request_reference() -> None:
    injected = "[[PYNCHY_REDACTED:attacker:1:EMAIL]]"
    source = f"Keep {injected}; contact real@example.test"
    session = RedactionSession()

    redacted = session.redact_text(source)
    restored = session.restore_text(
        redacted.value,
        _private_capability(SensitiveDataClass.EMAIL),
    )

    assert injected in redacted.value
    assert restored == source


def test_redaction_skips_a_placeholder_that_collides_with_the_next_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pynchy.redaction.secrets.token_hex", lambda _size: "fixed")
    session = RedactionSession()

    redacted = session.redact_text("[[PYNCHY_REDACTED:fixed:1:EMAIL]] real@example.test")

    assert redacted.refs[0].token.endswith(":2:EMAIL]]")


def test_redaction_mappings_are_isolated_per_request() -> None:
    first = RedactionSession()
    second = RedactionSession()
    first_redacted = first.redact_text("first@example.test")
    second_redacted = second.redact_text("second@example.test")
    authority = _private_capability(SensitiveDataClass.EMAIL)

    assert first_redacted.refs[0].token != second_redacted.refs[0].token
    assert second.restore_text(first_redacted.value, authority) == first_redacted.value
    assert first.restore_text(first_redacted.value, authority) == "first@example.test"


def test_restoration_requires_non_public_sink_and_matching_data_class() -> None:
    session = RedactionSession()
    redacted = session.redact_text("email me at private@example.test")
    public_sink = RestorationCapability(
        capability_id=CapabilityId("test.public.send"),
        exposure=SinkExposure.PUBLIC,
        allowed_data_classes=frozenset({SensitiveDataClass.EMAIL}),
    )

    with pytest.raises(RestorationDeniedError, match="Restoration denied"):
        session.restore_text(redacted.value, public_sink)
    with pytest.raises(RestorationDeniedError, match="Restoration denied"):
        session.restore_text(
            redacted.value,
            _private_capability(SensitiveDataClass.PHONE),
        )


def test_complete_streamed_request_redacts_system_prompt_and_tool_results() -> None:
    secret = _credential()
    body = json.dumps(
        {
            "model": "test-model",
            "system": f"Account owner: owner@example.test; token={secret}",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Call 415-555-2671",
                            "metadata": {"attempts": 1},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "SSN 123-45-6789",
                        },
                    ],
                }
            ],
        }
    ).encode()
    chunks = (body[:17], body[17:53], body[53:89], body[89:])

    request = redact_llm_request_body(b"".join(chunks))
    forwarded = request.body.decode()

    assert request.redaction_count == 4
    assert secret not in forwarded
    assert "owner@example.test" not in forwarded
    assert "415-555-2671" not in forwarded
    assert "123-45-6789" not in forwarded
    assert forwarded.count("[[PYNCHY_REDACTED:") == 4


def test_invalid_request_error_never_echoes_input() -> None:
    source = f'{{"messages":["token={_credential()}"'

    with pytest.raises(RedactionRequestError) as caught:
        redact_llm_request_body(source.encode())

    assert _credential() not in str(caught.value)
    assert repr(RedactionSession()) == "RedactionSession(redaction_count=0)"

    with pytest.raises(RedactionRequestError, match="JSON object"):
        redact_llm_request_body(b"[]")


def test_production_body_transform_returns_no_restoration_session() -> None:
    secret = _credential()
    body = json.dumps({"messages": [{"role": "user", "content": f"token={secret}"}]}).encode()

    transformed = irreversibly_redact_llm_request_body(body)

    assert isinstance(transformed, bytes)
    assert secret.encode() not in transformed
