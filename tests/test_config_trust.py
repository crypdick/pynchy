"""Tests for trust-model config parsing."""

import pytest
from conftest import make_settings
from pydantic import ValidationError

from pynchy.config.merge import merge_sandbox_config
from pynchy.config.models import (
    CapabilityTomlConfig,
    ProfileConfig,
    ServiceTrustTomlConfig,
    WorkspaceConfig,
    WorkspaceSecurityTomlConfig,
    WorkspaceServiceOverride,
)


def test_service_trust_toml_defaults():
    """Unpopulated service trust config is maximally cautious."""
    cfg = ServiceTrustTomlConfig()
    assert cfg.public_source is True
    assert cfg.secret_data is True
    assert cfg.public_sink is True
    assert cfg.dangerous_writes is True


def test_service_trust_toml_all_false():
    """All-false config parses correctly."""
    cfg = ServiceTrustTomlConfig(
        public_source=False,
        secret_data=False,
        public_sink=False,
        dangerous_writes=False,
    )
    assert cfg.public_source is False
    assert cfg.dangerous_writes is False


def test_service_trust_toml_forbidden():
    """Forbidden string value parses correctly."""
    cfg = ServiceTrustTomlConfig(
        public_source="forbidden",
        public_sink="forbidden",
        dangerous_writes="forbidden",
    )
    assert cfg.public_source == "forbidden"


def test_service_trust_toml_invalid_value():
    """Invalid value raises ValidationError."""
    with pytest.raises(ValidationError):
        ServiceTrustTomlConfig(public_source="maybe")


def test_workspace_security_toml_defaults():
    cfg = WorkspaceSecurityTomlConfig()
    assert cfg.services == {}


def test_workspace_service_override_only_forbidden():
    """Workspace overrides only accept 'forbidden' values."""
    override = WorkspaceServiceOverride(public_sink="forbidden")
    assert override.public_sink == "forbidden"


def test_workspace_service_override_rejects_non_forbidden():
    """Workspace overrides reject values other than 'forbidden' or None."""
    with pytest.raises(ValidationError):
        WorkspaceServiceOverride(public_sink=True)


def test_workspace_config_has_security():
    """WorkspaceConfig accepts the new security config."""
    cfg = WorkspaceConfig(
        security=WorkspaceSecurityTomlConfig(
            services={"email": ServiceTrustTomlConfig(public_source=True)},
        ),
    )
    assert cfg.security is not None
    assert "email" in cfg.security.services


def test_profile_contains_secrets_feeds_runtime_security(monkeypatch):
    from pynchy.host.container_manager.security.gate import resolve_security

    settings = make_settings(
        profiles={"worker": ProfileConfig(contains_secrets=True)},
        workspaces={"research": WorkspaceConfig(profile="worker")},
    )
    monkeypatch.setattr("pynchy.config.get_settings", lambda: settings)

    assert resolve_security("research").contains_secrets is True


def test_capability_config_accepts_decisions():
    cfg = CapabilityTomlConfig(decision="needs_human")
    assert cfg.decision == "needs_human"


def test_sandbox_capabilities_merge_by_name_with_workspace_winning():
    universal = ProfileConfig(
        capabilities={
            "mcp.email.send": CapabilityTomlConfig(decision="needs_human"),
            "mcp.browser.*": CapabilityTomlConfig(decision="deny"),
        }
    )
    profile = ProfileConfig(capabilities={"mcp.email.send": CapabilityTomlConfig(decision="deny")})
    sandbox = WorkspaceConfig(
        capabilities={"mcp.email.send": CapabilityTomlConfig(decision="allow")}
    )

    resolved = merge_sandbox_config(universal, profile, sandbox)

    assert resolved.capabilities["mcp.email.send"].decision == "allow"
    assert resolved.capabilities["mcp.browser.*"].decision == "deny"
