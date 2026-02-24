"""Tests for ServiceTrustConfig and WorkspaceSecurity types."""

from pynchy.types import ServiceTrustConfig, WorkspaceProfile, WorkspaceSecurity

# --- ServiceTrustConfig defaults ---


def test_service_trust_defaults_maximally_restrictive():
    """Default ServiceTrustConfig is maximally cautious."""
    trust = ServiceTrustConfig()
    assert trust.public_source is True
    assert trust.secret_data is True
    assert trust.public_sink is True
    assert trust.dangerous_writes is True


def test_service_trust_fully_safe():
    """All-false config means no gating."""
    trust = ServiceTrustConfig(
        public_source=False,
        secret_data=False,
        public_sink=False,
        dangerous_writes=False,
    )
    assert trust.public_source is False
    assert trust.secret_data is False
    assert trust.public_sink is False
    assert trust.dangerous_writes is False


def test_service_trust_forbidden():
    """Forbidden values block the capability entirely."""
    trust = ServiceTrustConfig(
        public_source="forbidden",
        public_sink="forbidden",
        dangerous_writes="forbidden",
    )
    assert trust.public_source == "forbidden"
    assert trust.public_sink == "forbidden"
    assert trust.dangerous_writes == "forbidden"


# --- WorkspaceSecurity ---


def test_workspace_security_defaults():
    """Default WorkspaceSecurity has no services and no secrets."""
    sec = WorkspaceSecurity()
    assert sec.services == {}
    assert sec.contains_secrets is False


def test_workspace_security_with_services():
    """WorkspaceSecurity holds per-service trust configs."""
    sec = WorkspaceSecurity(
        services={
            "calendar": ServiceTrustConfig(
                public_source=False,
                secret_data=False,
                public_sink=False,
                dangerous_writes=False,
            ),
            "email": ServiceTrustConfig(
                public_source=True,
                secret_data=True,
                public_sink=True,
                dangerous_writes=True,
            ),
        },
        contains_secrets=True,
    )
    assert len(sec.services) == 2
    assert sec.services["calendar"].public_source is False
    assert sec.services["email"].public_source is True
    assert sec.contains_secrets is True


# --- WorkspaceProfile integration ---


def test_workspace_profile_uses_new_security():
    """WorkspaceProfile.security is WorkspaceSecurity with service trust."""
    profile = WorkspaceProfile(
        jid="test@g.us",
        name="Test",
        folder="test",
        trigger="@P",
        security=WorkspaceSecurity(
            services={"email": ServiceTrustConfig(public_source=True)},
        ),
    )
    assert "email" in profile.security.services
    assert profile.security.services["email"].public_source is True


def test_workspace_profile_validation_basic():
    """Basic validation still checks name/folder/trigger."""
    profile = WorkspaceProfile(
        jid="test@g.us",
        name="",
        folder="",
        trigger="@P",
    )
    errors = profile.validate()
    assert any("name" in e for e in errors)
    assert any("folder" in e for e in errors)
