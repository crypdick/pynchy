"""Tests for ServiceTrustConfig and WorkspaceSecurity types."""

import pytest

from pynchy.workspace.api import (
    CapabilityRule,
    ContainerConfig,
    RuntimeTarget,
    ServiceTrustConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
    capability_pattern_matches,
    most_restrictive_capability_rule,
)

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


def test_workspace_profile_requires_a_trigger():
    profile = WorkspaceProfile(
        jid="test@g.us",
        name="Test",
        folder="test",
        trigger="",
    )

    assert profile.validate() == ["Workspace trigger is required"]


def test_workspace_profile_validates_and_builds_its_runtime_target():
    profile = WorkspaceProfile(
        jid="slack:C123",
        name="Operations",
        folder="operations",
        trigger="@P",
    )

    assert profile.validate() == []
    assert RuntimeTarget.from_workspace(profile) == RuntimeTarget.from_binding(
        "operations", "slack:C123"
    )
    assert RuntimeTarget.from_workspace(profile).id == "operations"


def test_container_config_parses_mounts_and_rejects_invalid_shapes():
    config = ContainerConfig.from_dict(
        {
            "timeout": 30,
            "additional_mounts": [{"host_path": "/data", "container_path": "/workspace/data"}],
        }
    )

    assert config.timeout == 30
    assert config.additional_mounts[0].container_path == "/workspace/data"

    with pytest.raises(TypeError, match="timeout"):
        ContainerConfig.from_dict({"timeout": "slow"})
    with pytest.raises(TypeError, match="additional_mounts"):
        ContainerConfig.from_dict({"additional_mounts": "not-a-list"})


def test_capability_matching_and_restrictive_intersection():
    assert capability_pattern_matches("*", "mcp.calendar.create")
    assert capability_pattern_matches("mcp.calendar.*", "mcp.calendar.create")
    assert capability_pattern_matches("mcp.calendar.read", "mcp.calendar.read")
    assert not capability_pattern_matches("mcp.calendar.*", "mcp.email.send")

    rule = most_restrictive_capability_rule(
        [CapabilityRule("allow"), CapabilityRule("needs_human"), CapabilityRule("deny")]
    )

    assert rule == CapabilityRule("deny")
    assert most_restrictive_capability_rule([]) is None
