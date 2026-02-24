"""Deterministic payload secrets scanner using detect-secrets.

Scans outbound write payloads for leaked secrets (API keys, tokens,
private keys, etc.). Non-LLM, non-AI — purely rule-based detection.
Used by SecurityPolicy.evaluate_write() to escalate gating when
secrets are found in payloads regardless of taint state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from detect_secrets.core.scan import scan_line
from detect_secrets.settings import transient_settings

# Pattern-based detectors only — no high-entropy string detectors.
# High-entropy detectors (Base64HighEntropyString, HexHighEntropyString)
# produce too many false positives on normal prose.
_SCANNER_CONFIG = {
    "plugins_used": [
        {"name": "AWSKeyDetector"},
        {"name": "ArtifactoryDetector"},
        {"name": "AzureStorageKeyDetector"},
        {"name": "BasicAuthDetector"},
        {"name": "CloudantDetector"},
        {"name": "DiscordBotTokenDetector"},
        {"name": "GitHubTokenDetector"},
        {"name": "GitLabTokenDetector"},
        {"name": "IbmCloudIamDetector"},
        {"name": "IbmCosHmacDetector"},
        {"name": "JwtTokenDetector"},
        {"name": "MailchimpDetector"},
        {"name": "NpmDetector"},
        {"name": "PrivateKeyDetector"},
        {"name": "SendGridDetector"},
        {"name": "SlackDetector"},
        {"name": "SoftlayerDetector"},
        {"name": "SquareOAuthDetector"},
        {"name": "StripeDetector"},
        {"name": "TwilioKeyDetector"},
    ],
}


@dataclass
class ScanResult:
    """Result of scanning a payload for secrets."""

    secrets_found: bool = False
    detected: list[str] = field(default_factory=list)  # types of secrets found


def _payload_to_text(payload: str | dict | None) -> str:
    """Convert a payload to scannable text."""
    if payload is None:
        return ""
    if isinstance(payload, dict):
        return json.dumps(payload, default=str)
    return str(payload)


def scan_payload_for_secrets(payload: str | dict | None) -> ScanResult:
    """Scan a payload for secrets using detect-secrets.

    Returns a ScanResult indicating whether secrets were found
    and what types were detected.
    """
    text = _payload_to_text(payload)
    if not text.strip():
        return ScanResult()

    detected_types: list[str] = []
    with transient_settings(_SCANNER_CONFIG):
        for line in text.splitlines():
            for secret in scan_line(line):
                detected_types.append(secret.type)

    if not detected_types:
        return ScanResult()

    return ScanResult(
        secrets_found=True,
        detected=detected_types,
    )
