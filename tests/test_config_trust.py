"""Tests for trust-model config parsing."""

import pytest
from pydantic import ValidationError

from pynchy.config.models import (
    ServiceTrustTomlConfig,
    WorkspaceSecurityTomlConfig,
    WorkspaceServiceOverride,
)
from pynchy.config.settings import validate_settings_mapping


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


def test_admin_profile_with_public_source_tool_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Admin workspace"):
        validate_settings_mapping(
            {
                "profiles": {"admin": {"is_admin": True, "tools": ["browser"]}},
                "workspaces": {"admin": {"profiles": ["admin"]}},
                "tools": {"browser": {"type": "builtin", "name": "browser", "public_source": True}},
            }
        )


def test_admin_profile_with_non_public_source_tool_passes() -> None:
    settings = validate_settings_mapping(
        {
            "profiles": {"admin": {"is_admin": True, "tools": ["shell"]}},
            "workspaces": {"admin": {"profiles": ["admin"]}},
            "tools": {"shell": {"type": "builtin", "name": "shell", "public_source": False}},
        }
    )

    assert settings.resolved_workspace_config("admin").tools == ["shell"]
