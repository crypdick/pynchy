"""Tests for WorkspaceProfile and workspace security features (Phase B.1)."""

from pynchy.types import (
    ContainerConfig,
    McpToolConfig,
    RateLimitConfig,
    RegisteredGroup,
    WorkspaceProfile,
    WorkspaceSecurity,
)


def test_workspace_security_defaults():
    """Test WorkspaceSecurity with default values."""
    security = WorkspaceSecurity()

    assert security.mcp_tools == {}
    assert security.default_risk_tier == "human-approval"
    assert security.rate_limits is None
    assert security.allow_filesystem_access is True
    assert security.allow_network_access is True


def test_mcp_tool_config():
    """Test McpToolConfig creation."""
    config = McpToolConfig(risk_tier="always-approve", enabled=True)

    assert config.risk_tier == "always-approve"
    assert config.enabled is True


def test_workspace_profile_minimal():
    """Test WorkspaceProfile with minimal required fields."""
    profile = WorkspaceProfile(
        jid="1234567890@g.us",
        name="Test Workspace",
        folder="test-workspace",
        trigger="@Pynchy",
    )

    assert profile.jid == "1234567890@g.us"
    assert profile.name == "Test Workspace"
    assert profile.folder == "test-workspace"
    assert profile.trigger == "@Pynchy"
    assert profile.requires_trigger is True  # Default
    assert profile.container_config is None
    assert isinstance(profile.security, WorkspaceSecurity)
    assert profile.added_at == ""


def test_workspace_profile_with_security():
    """Test WorkspaceProfile with custom security configuration."""
    security = WorkspaceSecurity(
        mcp_tools={
            "read_email": McpToolConfig(risk_tier="always-approve"),
            "send_email": McpToolConfig(risk_tier="human-approval"),
            "create_event": McpToolConfig(risk_tier="rules-engine"),
        },
        default_risk_tier="rules-engine",
        allow_filesystem_access=True,
        allow_network_access=False,
    )

    profile = WorkspaceProfile(
        jid="1234567890@g.us",
        name="Banking Workspace",
        folder="banking",
        trigger="@Pynchy",
        security=security,
    )

    assert len(profile.security.mcp_tools) == 3
    assert profile.security.mcp_tools["read_email"].risk_tier == "always-approve"
    assert profile.security.mcp_tools["send_email"].risk_tier == "human-approval"
    assert profile.security.mcp_tools["create_event"].risk_tier == "rules-engine"
    assert profile.security.default_risk_tier == "rules-engine"
    assert profile.security.allow_network_access is False


def test_workspace_profile_validation_success():
    """Test that valid profiles pass validation."""
    profile = WorkspaceProfile(
        jid="1234567890@g.us",
        name="Valid Workspace",
        folder="valid",
        trigger="@Pynchy",
        security=WorkspaceSecurity(
            mcp_tools={
                "read_email": McpToolConfig(risk_tier="always-approve"),
            }
        ),
    )

    errors = profile.validate()
    assert errors == []


def test_workspace_profile_validation_missing_name():
    """Test validation fails when name is missing."""
    profile = WorkspaceProfile(
        jid="1234567890@g.us",
        name="",  # Empty
        folder="test",
        trigger="@Pynchy",
    )

    errors = profile.validate()
    assert len(errors) == 1
    assert "name is required" in errors[0]


def test_workspace_profile_validation_missing_folder():
    """Test validation fails when folder is missing."""
    profile = WorkspaceProfile(
        jid="1234567890@g.us",
        name="Test",
        folder="",  # Empty
        trigger="@Pynchy",
    )

    errors = profile.validate()
    assert len(errors) == 1
    assert "folder is required" in errors[0]


def test_workspace_profile_validation_missing_trigger():
    """Test validation fails when trigger is missing."""
    profile = WorkspaceProfile(
        jid="1234567890@g.us",
        name="Test",
        folder="test",
        trigger="",  # Empty
    )

    errors = profile.validate()
    assert len(errors) == 1
    assert "trigger is required" in errors[0]


def test_workspace_profile_validation_invalid_risk_tier():
    """Test validation fails for invalid risk tiers."""
    profile = WorkspaceProfile(
        jid="1234567890@g.us",
        name="Test",
        folder="test",
        trigger="@Pynchy",
        security=WorkspaceSecurity(
            mcp_tools={
                "bad_tool": McpToolConfig(risk_tier="invalid-tier"),  # type: ignore
            }
        ),
    )

    errors = profile.validate()
    assert len(errors) == 1
    assert "Invalid risk tier 'invalid-tier'" in errors[0]
    assert "bad_tool" in errors[0]


def test_workspace_profile_validation_invalid_default_tier():
    """Test validation fails for invalid default risk tier."""
    profile = WorkspaceProfile(
        jid="1234567890@g.us",
        name="Test",
        folder="test",
        trigger="@Pynchy",
        security=WorkspaceSecurity(
            default_risk_tier="super-dangerous",  # type: ignore
        ),
    )

    errors = profile.validate()
    assert len(errors) == 1
    assert "Invalid default risk tier" in errors[0]


def test_workspace_profile_validation_multiple_errors():
    """Test validation returns multiple errors."""
    profile = WorkspaceProfile(
        jid="1234567890@g.us",
        name="",  # Missing
        folder="",  # Missing
        trigger="@Pynchy",
        security=WorkspaceSecurity(
            mcp_tools={
                "tool1": McpToolConfig(risk_tier="bad"),  # type: ignore
            },
            default_risk_tier="also-bad",  # type: ignore
        ),
    )

    errors = profile.validate()
    assert len(errors) == 4  # name, folder, tool1 tier, default tier


def test_workspace_profile_from_registered_group():
    """Test migration from RegisteredGroup to WorkspaceProfile."""
    rg = RegisteredGroup(
        name="Old Group",
        folder="old-group",
        trigger="@Bot",
        added_at="2024-01-01T00:00:00Z",
        requires_trigger=False,
        container_config=ContainerConfig(timeout=600.0),
    )

    profile = WorkspaceProfile.from_registered_group("12345@g.us", rg)

    assert profile.jid == "12345@g.us"
    assert profile.name == "Old Group"
    assert profile.folder == "old-group"
    assert profile.trigger == "@Bot"
    assert profile.requires_trigger is False
    assert profile.added_at == "2024-01-01T00:00:00Z"
    assert profile.container_config is not None
    assert profile.container_config.timeout == 600.0
    # Default security should be applied
    assert isinstance(profile.security, WorkspaceSecurity)
    assert profile.security.default_risk_tier == "human-approval"


def test_workspace_profile_to_registered_group():
    """Test conversion from WorkspaceProfile to RegisteredGroup (backward compat)."""
    profile = WorkspaceProfile(
        jid="12345@g.us",
        name="New Workspace",
        folder="new-workspace",
        trigger="@Pynchy",
        requires_trigger=True,
        added_at="2024-01-01T00:00:00Z",
        container_config=ContainerConfig(timeout=300.0),
        security=WorkspaceSecurity(
            mcp_tools={
                "read_email": McpToolConfig(risk_tier="always-approve"),
            }
        ),
    )

    rg = profile.to_registered_group()

    assert rg.name == "New Workspace"
    assert rg.folder == "new-workspace"
    assert rg.trigger == "@Pynchy"
    assert rg.requires_trigger is True
    assert rg.added_at == "2024-01-01T00:00:00Z"
    assert rg.container_config is not None
    assert rg.container_config.timeout == 300.0
    # Security info is lost in conversion (expected)


def test_workspace_profile_roundtrip():
    """Test RegisteredGroup -> WorkspaceProfile -> RegisteredGroup roundtrip."""
    original_rg = RegisteredGroup(
        name="Test Group",
        folder="test-group",
        trigger="@Bot",
        added_at="2024-01-01T00:00:00Z",
        requires_trigger=True,
    )

    # Convert to WorkspaceProfile
    profile = WorkspaceProfile.from_registered_group("12345@g.us", original_rg)

    # Convert back to RegisteredGroup
    final_rg = profile.to_registered_group()

    # Should match original (except security is not preserved)
    assert final_rg.name == original_rg.name
    assert final_rg.folder == original_rg.folder
    assert final_rg.trigger == original_rg.trigger
    assert final_rg.added_at == original_rg.added_at
    assert final_rg.requires_trigger == original_rg.requires_trigger


def test_workspace_security_with_mixed_tool_states():
    """Test security config with enabled and disabled tools."""
    security = WorkspaceSecurity(
        mcp_tools={
            "read_email": McpToolConfig(risk_tier="always-approve", enabled=True),
            "send_email": McpToolConfig(risk_tier="human-approval", enabled=False),
            "get_password": McpToolConfig(risk_tier="human-approval", enabled=True),
        }
    )

    profile = WorkspaceProfile(
        jid="12345@g.us",
        name="Mixed Security",
        folder="mixed",
        trigger="@Pynchy",
        security=security,
    )

    # Enabled tools
    assert profile.security.mcp_tools["read_email"].enabled is True
    assert profile.security.mcp_tools["get_password"].enabled is True

    # Disabled tool
    assert profile.security.mcp_tools["send_email"].enabled is False


# --- Rate limits ---


def test_rate_limit_config():
    """Test RateLimitConfig creation."""
    rl = RateLimitConfig(max_calls_per_hour=100, per_tool_overrides={"send_email": 5})
    assert rl.max_calls_per_hour == 100
    assert rl.per_tool_overrides == {"send_email": 5}


def test_rate_limit_config_defaults():
    """Test RateLimitConfig defaults."""
    rl = RateLimitConfig()
    assert rl.max_calls_per_hour == 500
    assert rl.per_tool_overrides == {}


def test_workspace_security_with_rate_limits():
    """Test WorkspaceSecurity with rate limits configured."""
    security = WorkspaceSecurity(
        rate_limits=RateLimitConfig(max_calls_per_hour=30, per_tool_overrides={"send_email": 5}),
    )

    profile = WorkspaceProfile(
        jid="12345@g.us",
        name="Rate Limited",
        folder="rate-limited",
        trigger="@Pynchy",
        security=security,
    )

    assert profile.security.rate_limits is not None
    assert profile.security.rate_limits.max_calls_per_hour == 30
    assert profile.security.rate_limits.per_tool_overrides["send_email"] == 5


def test_workspace_profile_validation_rate_limits_valid():
    """Test validation passes for valid rate limits."""
    profile = WorkspaceProfile(
        jid="12345@g.us",
        name="Test",
        folder="test",
        trigger="@Pynchy",
        security=WorkspaceSecurity(
            rate_limits=RateLimitConfig(
                max_calls_per_hour=100,
                per_tool_overrides={"send_email": 10},
            ),
        ),
    )
    errors = profile.validate()
    assert errors == []


def test_workspace_profile_validation_rate_limits_invalid_max():
    """Test validation fails for non-positive max_calls_per_hour."""
    profile = WorkspaceProfile(
        jid="12345@g.us",
        name="Test",
        folder="test",
        trigger="@Pynchy",
        security=WorkspaceSecurity(
            rate_limits=RateLimitConfig(max_calls_per_hour=0),
        ),
    )
    errors = profile.validate()
    assert len(errors) == 1
    assert "max_calls_per_hour" in errors[0]


def test_workspace_profile_validation_rate_limits_invalid_per_tool():
    """Test validation fails for non-positive per-tool override."""
    profile = WorkspaceProfile(
        jid="12345@g.us",
        name="Test",
        folder="test",
        trigger="@Pynchy",
        security=WorkspaceSecurity(
            rate_limits=RateLimitConfig(
                max_calls_per_hour=100,
                per_tool_overrides={"send_email": -1},
            ),
        ),
    )
    errors = profile.validate()
    assert len(errors) == 1
    assert "per-tool rate limit" in errors[0]


def test_workspace_profile_validation_rate_limits_none():
    """Test validation passes when rate_limits is None."""
    profile = WorkspaceProfile(
        jid="12345@g.us",
        name="Test",
        folder="test",
        trigger="@Pynchy",
        security=WorkspaceSecurity(rate_limits=None),
    )
    errors = profile.validate()
    assert errors == []
