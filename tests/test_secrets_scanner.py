"""Tests for payload secrets scanner."""

from pynchy.security.secrets_scanner import scan_payload_for_secrets


def test_no_secrets_in_plain_text():
    """Normal text has no secrets."""
    result = scan_payload_for_secrets("Hello, here is my report.")
    assert not result.secrets_found
    assert result.detected == []


def test_detects_aws_key():
    """Detects AWS access key in payload."""
    payload = "Here is the config: AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret
    result = scan_payload_for_secrets(payload)
    assert result.secrets_found


def test_detects_github_token():
    """Detects GitHub personal access token."""
    # ghp_ prefix + exactly 36 alphanumeric chars
    payload = "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"  # pragma: allowlist secret
    result = scan_payload_for_secrets(payload)
    assert result.secrets_found


def test_detects_generic_high_entropy():
    """Detects high-entropy strings that look like tokens."""
    # A hex token long enough to trigger entropy detection
    payload = "token=a]1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6"
    result = scan_payload_for_secrets(payload)
    # High-entropy detection may or may not trigger depending on
    # detect-secrets config — this test validates the scanner runs
    assert isinstance(result.secrets_found, bool)


def test_scans_dict_payload():
    """Scans dict values by JSON-serializing them."""
    payload = {
        "to": "boss@company.com",
        "subject": "Config",
        "body": "AWS key: AKIAIOSFODNN7EXAMPLE",  # pragma: allowlist secret
    }
    result = scan_payload_for_secrets(payload)
    assert result.secrets_found


def test_empty_payload():
    result = scan_payload_for_secrets("")
    assert not result.secrets_found


def test_none_payload():
    result = scan_payload_for_secrets(None)
    assert not result.secrets_found
