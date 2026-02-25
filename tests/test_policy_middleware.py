"""Tests for SecurityPolicy — the trust-based gating engine."""

from pynchy.security.middleware import PolicyDecision, SecurityPolicy
from pynchy.types import ServiceTrustConfig, WorkspaceSecurity

# --- Helpers ---


def _make_policy(**services: ServiceTrustConfig) -> SecurityPolicy:
    """Create a SecurityPolicy with given services."""
    return SecurityPolicy(WorkspaceSecurity(services=dict(services)))


def _make_policy_with_secrets(**services: ServiceTrustConfig) -> SecurityPolicy:
    return SecurityPolicy(WorkspaceSecurity(services=dict(services), contains_secrets=True))


# --- PolicyDecision ---


def test_policy_decision_defaults():
    d = PolicyDecision(allowed=True)
    assert d.allowed is True
    assert d.reason is None
    assert d.needs_cop is False
    assert d.needs_human is False


# --- Forbidden blocks unconditionally ---


def test_forbidden_source_blocks_read():
    policy = _make_policy(email=ServiceTrustConfig(public_source="forbidden"))
    decision = policy.evaluate_read("email")
    assert decision.allowed is False
    assert "forbidden" in decision.reason.lower()


def test_forbidden_sink_blocks_write():
    policy = _make_policy(email=ServiceTrustConfig(public_sink="forbidden"))
    decision = policy.evaluate_write("email", {})
    assert decision.allowed is False


def test_forbidden_writes_blocks_write():
    policy = _make_policy(
        email=ServiceTrustConfig(public_sink=False, dangerous_writes="forbidden"),
    )
    decision = policy.evaluate_write("email", {})
    assert decision.allowed is False


# --- Read gating + corruption taint ---


def test_read_trusted_source_no_taint():
    """Reading from trusted source: no scanning, no taint."""
    policy = _make_policy(calendar=ServiceTrustConfig(public_source=False))
    decision = policy.evaluate_read("calendar")
    assert decision.allowed is True
    assert decision.needs_cop is False
    assert not policy.corruption_tainted


def test_read_public_source_taints():
    """Reading from public source: cop scan + corruption taint."""
    policy = _make_policy(email=ServiceTrustConfig(public_source=True))
    decision = policy.evaluate_read("email")
    assert decision.allowed is True
    assert decision.needs_cop is True
    assert policy.corruption_tainted


# --- Secret taint ---


def test_read_secret_data_sets_secret_taint():
    """Reading from service with secret_data=True sets secret taint."""
    policy = _make_policy(passwords=ServiceTrustConfig(secret_data=True, public_source=False))
    policy.evaluate_read("passwords")
    assert policy.secret_tainted


def test_read_non_secret_no_secret_taint():
    policy = _make_policy(calendar=ServiceTrustConfig(secret_data=False, public_source=False))
    policy.evaluate_read("calendar")
    assert not policy.secret_tainted


def test_file_access_with_contains_secrets():
    """File access in workspace with contains_secrets=True sets secret taint."""
    policy = _make_policy_with_secrets()
    assert not policy.secret_tainted
    policy.notify_file_access()
    assert policy.secret_tainted


def test_file_access_without_contains_secrets():
    """File access in workspace without contains_secrets does not set secret taint."""
    policy = _make_policy()
    policy.notify_file_access()
    assert not policy.secret_tainted


# --- Write gating: no taint ---


def test_write_no_taint_no_dangerous_no_gating():
    """Untainted, dangerous_writes=False, public_sink=False -> no gating."""
    policy = _make_policy(
        calendar=ServiceTrustConfig(public_sink=False, dangerous_writes=False),
    )
    decision = policy.evaluate_write("calendar", {})
    assert decision.allowed is True
    assert not decision.needs_cop
    assert not decision.needs_human


def test_write_no_taint_dangerous_writes_human_only():
    """Untainted, dangerous_writes=True -> human confirmation only."""
    policy = _make_policy(
        email=ServiceTrustConfig(public_sink=True, dangerous_writes=True),
    )
    decision = policy.evaluate_write("email", {})
    assert decision.allowed is True
    assert not decision.needs_cop
    assert decision.needs_human


def test_write_no_taint_public_sink_no_dangerous_no_gating():
    """Untainted, public_sink=True, dangerous_writes=False -> no gating."""
    policy = _make_policy(
        reddit=ServiceTrustConfig(public_sink=True, dangerous_writes=False),
    )
    decision = policy.evaluate_write("reddit", {})
    assert decision.allowed is True
    assert not decision.needs_cop
    assert not decision.needs_human


# --- Write gating: corruption tainted ---


def test_write_corrupted_no_secret_no_public_sink_cop_only():
    """Corrupted, no secret taint, private sink -> cop only."""
    policy = _make_policy(
        web=ServiceTrustConfig(public_source=True),
        notes=ServiceTrustConfig(public_sink=False, dangerous_writes=False),
    )
    policy.evaluate_read("web")  # corruption taint
    decision = policy.evaluate_write("notes", {})
    assert decision.needs_cop
    assert not decision.needs_human


def test_write_corrupted_no_secret_public_sink_cop_only():
    """Corrupted, no secret taint, public sink -> cop only (no secrets to exfil)."""
    policy = _make_policy(
        web=ServiceTrustConfig(public_source=True, secret_data=False),
        reddit=ServiceTrustConfig(public_sink=True, dangerous_writes=False, secret_data=False),
    )
    policy.evaluate_read("web")  # corruption taint
    decision = policy.evaluate_write("reddit", {})
    assert decision.needs_cop
    assert not decision.needs_human  # no secret taint -> no full trifecta


def test_write_full_trifecta_cop_plus_human():
    """Corrupted + secret + public sink -> cop + human (full trifecta)."""
    policy = _make_policy(
        web=ServiceTrustConfig(public_source=True, secret_data=False),
        passwords=ServiceTrustConfig(secret_data=True, public_source=False),
        email=ServiceTrustConfig(public_sink=True, dangerous_writes=False),
    )
    policy.evaluate_read("web")  # corruption taint
    policy.evaluate_read("passwords")  # secret taint
    decision = policy.evaluate_write("email", {})
    assert decision.needs_cop
    assert decision.needs_human  # full trifecta!


def test_write_corrupted_dangerous_writes_cop_plus_human():
    """Corrupted + dangerous_writes -> cop + human."""
    policy = _make_policy(
        web=ServiceTrustConfig(public_source=True),
        db=ServiceTrustConfig(public_sink=False, dangerous_writes=True),
    )
    policy.evaluate_read("web")  # corruption taint
    decision = policy.evaluate_write("db", {})
    assert decision.needs_cop
    assert decision.needs_human


# --- Taint stickiness ---


def test_corruption_taint_is_sticky():
    """Once corruption-tainted, stays tainted for all subsequent operations."""
    policy = _make_policy(
        web=ServiceTrustConfig(public_source=True),
        calendar=ServiceTrustConfig(public_source=False),
    )
    policy.evaluate_read("web")  # taints
    policy.evaluate_read("calendar")  # safe read, but taint persists
    assert policy.corruption_tainted


def test_secret_taint_is_sticky():
    """Once secret-tainted, stays tainted."""
    policy = _make_policy(
        passwords=ServiceTrustConfig(secret_data=True, public_source=False),
        calendar=ServiceTrustConfig(secret_data=False, public_source=False),
    )
    policy.evaluate_read("passwords")  # secret taint
    policy.evaluate_read("calendar")
    assert policy.secret_tainted


# --- Unknown service uses maximally cautious defaults ---


def test_unknown_service_read_uses_cautious_defaults():
    """Reading from an unknown service treats it as public_source=True."""
    policy = _make_policy()
    decision = policy.evaluate_read("unknown_service")
    assert decision.needs_cop  # public_source=True default
    assert policy.corruption_tainted


def test_unknown_service_write_uses_cautious_defaults():
    """Writing to an unknown service treats it as dangerous_writes=True."""
    policy = _make_policy()
    decision = policy.evaluate_write("unknown_service", {})
    assert decision.needs_human  # dangerous_writes=True default


# --- Payload secrets scanner integration ---


def test_write_payload_with_secrets_escalates_to_human():
    """Payload containing secrets forces human confirmation even if untainted."""
    policy = _make_policy(
        email=ServiceTrustConfig(
            public_sink=True,
            dangerous_writes=False,
            public_source=False,
            secret_data=False,
        ),
    )
    data = {"body": "Here is the key: AKIAIOSFODNN7EXAMPLE"}  # pragma: allowlist secret
    decision = policy.evaluate_write("email", data)
    assert decision.needs_human  # escalated by secrets scanner
