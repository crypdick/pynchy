"""Local reversible redaction for Pynchy-owned LLM request boundaries."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from pynchy.identifiers import (
    CapabilityId,
)


class SensitiveDataClass(StrEnum):
    """Data classes recognized by the deterministic local detector."""

    CREDENTIAL = "credential"
    PRIVATE_KEY = "private_key"  # pragma: allowlist secret
    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    PAYMENT_CARD = "payment_card"


class GatewayRedactionPosture(StrEnum):
    """Whether Pynchy owns and enforces the active LLM request boundary."""

    ENFORCED = "enforced"
    NOT_ENFORCED = "not_enforced"


def redaction_posture_for_gateway_mode(mode: str) -> GatewayRedactionPosture:
    """Return the explicit request-redaction posture for a configured gateway mode."""
    if mode == "builtin":
        return GatewayRedactionPosture.ENFORCED
    if mode == "litellm":
        return GatewayRedactionPosture.NOT_ENFORCED
    raise ValueError(f"Unknown gateway mode: {mode}")


class SinkExposure(StrEnum):
    """Whether a restoration target can expose data to untrusted parties."""

    NON_PUBLIC = "non_public"
    PUBLIC = "public"  # noqa: V107


@dataclass(frozen=True)
class RedactionSpan:
    """Location and class of a match, without retaining its sensitive value."""

    start: int
    end: int
    data_class: SensitiveDataClass


@dataclass(frozen=True)
class PlaceholderRef:
    """Opaque reference to one request-local protected value."""

    token: str
    data_class: SensitiveDataClass


@dataclass(frozen=True)
class RedactedText:
    """Redacted text and the opaque references it contains."""

    value: str
    refs: tuple[PlaceholderRef, ...]


@dataclass(frozen=True)
class RestorationCapability:
    """Lower-level restoration policy for isolated tests and future trusted sinks.

    This caller-constructed value is not production authorization. No active
    gateway path retains a session or accepts this value for restoration.
    """

    capability_id: CapabilityId
    exposure: SinkExposure
    allowed_data_classes: frozenset[SensitiveDataClass]


@dataclass(frozen=True)
class RedactedLlmRequest:
    """One transformed LLM body and its isolated restoration session."""

    body: bytes
    session: RedactionSession
    redaction_count: int


@dataclass(frozen=True)
class _ProtectedOriginal:
    value: str
    data_class: SensitiveDataClass


@dataclass(frozen=True)
class _Detector:
    pattern: re.Pattern[str]
    data_class: SensitiveDataClass
    priority: int
    value_group: int = 0
    validator: str | None = None


class RedactionRequestError(ValueError):
    """An LLM request cannot be safely parsed for redaction."""


class RestorationDeniedError(ValueError):
    """A sink lacks authority to restore one or more protected values."""


_PLACEHOLDER_PATTERN = re.compile(r"\[\[PYNCHY_REDACTED:[^\]]+\]\]")
# NOTE: Update docs/architecture/security.md § LLM request redaction when
# changing the detected data classes or restoration boundary.
_DETECTORS = (
    _Detector(
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        SensitiveDataClass.PRIVATE_KEY,
        120,
    ),
    _Detector(
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|"
            r"secret|authorization)\b\s*[:=]\s*([^\s,;]+)"
        ),
        SensitiveDataClass.CREDENTIAL,
        110,
        value_group=1,
    ),
    _Detector(
        re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{8,})"),
        SensitiveDataClass.CREDENTIAL,
        110,
        value_group=1,
    ),
    _Detector(
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        SensitiveDataClass.CREDENTIAL,
        110,
    ),
    _Detector(
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}\b"),
        SensitiveDataClass.CREDENTIAL,
        110,
    ),
    _Detector(
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,255}\b"),
        SensitiveDataClass.CREDENTIAL,
        110,
    ),
    _Detector(
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        SensitiveDataClass.CREDENTIAL,
        110,
    ),
    _Detector(
        re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
        SensitiveDataClass.PAYMENT_CARD,
        90,
        validator="payment_card",
    ),
    _Detector(
        re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
        SensitiveDataClass.SSN,
        80,
    ),
    _Detector(
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b"),
        SensitiveDataClass.EMAIL,
        60,
    ),
    _Detector(
        re.compile(r"(?<!\w)(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\w)"),
        SensitiveDataClass.PHONE,
        50,
    ),
)


def _valid_payment_card(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        weighted_digit = digit
        if index % 2 == parity:
            weighted_digit *= 2
            if weighted_digit > 9:
                weighted_digit -= 9
        checksum += weighted_digit
    return checksum % 10 == 0


def detect_sensitive_spans(source: str) -> tuple[RedactionSpan, ...]:
    """Return prioritized, non-overlapping spans without captured values."""
    candidates: list[tuple[RedactionSpan, int]] = []
    for detector in _DETECTORS:
        for match in detector.pattern.finditer(source):
            start, end = match.span(detector.value_group)
            value = source[start:end]
            if detector.validator == "payment_card" and not _valid_payment_card(value):
                continue
            candidates.append(
                (
                    RedactionSpan(start=start, end=end, data_class=detector.data_class),
                    detector.priority,
                )
            )

    selected: list[RedactionSpan] = []
    for span, _priority in sorted(
        candidates,
        key=lambda item: (-item[1], -(item[0].end - item[0].start), item[0].start),
    ):
        if any(span.start < existing.end and existing.start < span.end for existing in selected):
            continue
        selected.append(span)
    return tuple(sorted(selected, key=lambda span: span.start))


class RedactionSession:
    """A request-local mapping that never exposes originals in diagnostics."""

    def __init__(self) -> None:
        self._namespace = secrets.token_hex(12)
        self._next_ref = 1
        self._originals: dict[str, _ProtectedOriginal] = {}
        self._reserved_tokens: set[str] = set()

    def __repr__(self) -> str:
        return f"RedactionSession(redaction_count={len(self._originals)})"

    @property
    def redaction_count(self) -> int:
        return len(self._originals)

    def redact_text(self, source: str) -> RedactedText:
        """Redact detected values and retain exact originals in this session."""
        self._reserved_tokens.update(_PLACEHOLDER_PATTERN.findall(source))
        spans = detect_sensitive_spans(source)
        if not spans:
            return RedactedText(value=source, refs=())

        pieces: list[str] = []
        refs: list[PlaceholderRef] = []
        cursor = 0
        for span in spans:
            token = self._allocate_token(span.data_class)
            pieces.extend((source[cursor : span.start], token))
            self._originals[token] = _ProtectedOriginal(
                value=source[span.start : span.end],
                data_class=span.data_class,
            )
            refs.append(PlaceholderRef(token=token, data_class=span.data_class))
            cursor = span.end
        pieces.append(source[cursor:])
        return RedactedText(value="".join(pieces), refs=tuple(refs))

    def restore_text(self, source: str, capability: RestorationCapability) -> str:  # noqa: V105
        """Exercise lower-level non-public restoration policy outside the gateway."""
        originals = self._authorized_originals(source, capability)
        restored = source
        for token, original in originals:
            restored = restored.replace(token, original.value)
        return restored

    def _allocate_token(self, data_class: SensitiveDataClass) -> str:
        while True:
            ordinal = self._next_ref
            self._next_ref += 1
            token = f"[[PYNCHY_REDACTED:{self._namespace}:{ordinal}:{data_class.value.upper()}]]"
            if token not in self._reserved_tokens:
                self._reserved_tokens.add(token)
                return token

    def _authorized_originals(
        self,
        source: str,
        capability: RestorationCapability,
    ) -> tuple[tuple[str, _ProtectedOriginal], ...]:
        present = tuple(
            (token, original) for token, original in self._originals.items() if token in source
        )
        authorized = capability.exposure is SinkExposure.NON_PUBLIC and all(
            original.data_class in capability.allowed_data_classes for _, original in present
        )
        if not authorized:
            raise RestorationDeniedError(
                "Restoration denied for this sink capability or data class"
            )
        return present


def irreversibly_redact(source: str) -> str:
    """Return a safe diagnostic string without retaining a restoration map."""
    session = RedactionSession()
    redacted = session.redact_text(source)
    if not redacted.refs:
        return source
    classes = sorted({ref.data_class.value for ref in redacted.refs})
    return f"[redacted sensitive data: {', '.join(classes)}]"


def redact_llm_request_body(body: bytes) -> RedactedLlmRequest:
    """Return lower-level redaction state for tests and future trusted sinks."""
    try:
        payload = cast("object", json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RedactionRequestError("LLM request body must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise RedactionRequestError("LLM request body must be a JSON object")

    session = RedactionSession()
    redacted_payload = cast("dict[str, object]", dict(payload))
    for field in ("system", "instructions", "prompt", "input", "messages", "tool_results"):
        if field in redacted_payload:
            redacted_payload[field] = _redact_content(redacted_payload[field], session)
    redacted_body = (
        body
        if session.redaction_count == 0
        else json.dumps(redacted_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return RedactedLlmRequest(
        body=redacted_body,
        session=session,
        redaction_count=session.redaction_count,
    )


def irreversibly_redact_llm_request_body(body: bytes) -> bytes:
    """Redact a production gateway body and discard its request-local mapping."""
    return redact_llm_request_body(body).body


def _redact_content(value: object, session: RedactionSession) -> object:
    if isinstance(value, str):
        return session.redact_text(value).value
    if isinstance(value, list):
        return [_redact_content(item, session) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_content(item, session) for key, item in value.items()}
    return value
