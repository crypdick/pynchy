# Security Hardening: Step 1 - Workspace Security Profiles

## Overview

Establish the security profile schema and configuration system that defines which MCP tools each workspace can access and their associated risk tiers.

## Scope

This step creates the foundational security configuration layer without implementing any actual service integrations. It's purely about defining the security model and making it configurable per workspace.

## Dependencies

- ✅ Current workspace/group system (already implemented)
- ✅ IPC MCP system (already implemented)

## Background: The Lethal Trifecta

The orchestrator (agent) has:
- **A) Untrusted input** (user messages, emails, web content)
- **B) Sensitive data** (passwords, banking info, personal calendar)
- **C) External communications** (email, WhatsApp, banking APIs)

Having all three is dangerous. **We gate C** with deterministic host-side controls. The agent cannot bypass these gates - they're enforced by the host process, not by the LLM.

## Risk Tiers

| Tier | Gating | Examples |
|------|--------|---------|
| **Read-only** | Auto-approved | `read_email`, `list_calendar`, `bank_balance`, `search_web` |
| **Write** | Policy check (rules engine) | `create_event`, `update_task`, `archive_email` |
| **External / destructive** | Human approval via WhatsApp | `send_email`, `get_password`, `bank_transfer`, `delete_email` |

The policy check tier uses a **deterministic rules engine** (not an LLM): e.g., "create_event is OK if the calendar is the user's own." Human approval is the final gate for high-risk actions.

## Implementation

### 1. Define Security Profile Schema

**File:** `src/pynchy/types/security.py` (new file)

```python
"""Security profile types for workspace isolation."""

from __future__ import annotations

from enum import Enum
from typing import TypedDict


class RiskTier(str, Enum):
    """Risk tiers for MCP tools."""

    READ_ONLY = "read_only"  # Auto-approved
    WRITE = "write"  # Policy check via rules engine
    EXTERNAL = "external"  # Human approval required


class ToolProfile(TypedDict):
    """Security profile for a single MCP tool."""

    tier: RiskTier
    enabled: bool


class RateLimitConfig(TypedDict):
    """Rate limiting configuration for a workspace."""

    max_calls_per_hour: int  # Global limit across all tools
    per_tool_overrides: dict[str, int]  # tool_name -> max_calls_per_hour


class WorkspaceSecurityProfile(TypedDict):
    """Security configuration for a workspace."""

    tools: dict[str, ToolProfile]  # tool_name -> profile
    default_tier: RiskTier  # Default for undefined tools
    allow_unknown_tools: bool  # Whether to allow tools not in profile
    rate_limits: RateLimitConfig | None  # Rate limiting (None = no limits)
```

### 2. Update Group Config Schema

**File:** `src/pynchy/types/group.py`

Add to `GroupConfig`:

```python
from pynchy.types.security import WorkspaceSecurityProfile

class GroupConfig(TypedDict):
    # ... existing fields ...
    security_profile: WorkspaceSecurityProfile | None  # New field
```

### 3. Create Default Security Profiles

**File:** `src/pynchy/config/security_defaults.py` (new file)

```python
"""Default security profiles for workspaces."""

from pynchy.types.security import RiskTier, WorkspaceSecurityProfile

# Conservative default: all tools require approval, tight rate limits
STRICT_PROFILE: WorkspaceSecurityProfile = {
    "tools": {},
    "default_tier": RiskTier.EXTERNAL,
    "allow_unknown_tools": False,
    "rate_limits": {
        "max_calls_per_hour": 60,
        "per_tool_overrides": {},
    },
}

# Permissive profile for trusted workspaces (like 'main')
TRUSTED_PROFILE: WorkspaceSecurityProfile = {
    "tools": {
        # Messaging tools (current system)
        "send_message": {"tier": RiskTier.READ_ONLY, "enabled": True},
        "schedule_task": {"tier": RiskTier.WRITE, "enabled": True},
        # Future tools will be added here
    },
    "default_tier": RiskTier.WRITE,
    "allow_unknown_tools": True,  # Allow experimentation
    "rate_limits": {
        "max_calls_per_hour": 500,
        "per_tool_overrides": {},
    },
}


def get_default_profile(workspace_name: str) -> WorkspaceSecurityProfile:
    """Return appropriate default profile for workspace."""
    if workspace_name == "main":
        return TRUSTED_PROFILE
    return STRICT_PROFILE
```

### 4. Add Profile Validation

**File:** `src/pynchy/config/validation.py` (new or extend existing)

```python
"""Validate security profiles on startup."""

from pynchy.types.security import RiskTier, WorkspaceSecurityProfile


class SecurityProfileError(Exception):
    """Raised when security profile is invalid."""


def validate_security_profile(profile: WorkspaceSecurityProfile) -> None:
    """Validate a security profile configuration.

    Raises:
        SecurityProfileError: If profile is invalid
    """
    # Check default tier is valid
    if profile["default_tier"] not in RiskTier:
        raise SecurityProfileError(f"Invalid default_tier: {profile['default_tier']}")

    # Check all tool profiles
    for tool_name, tool_profile in profile["tools"].items():
        tier = tool_profile["tier"]
        if tier not in RiskTier:
            raise SecurityProfileError(f"Invalid tier for {tool_name}: {tier}")

        enabled = tool_profile["enabled"]
        if not isinstance(enabled, bool):
            raise SecurityProfileError(f"Invalid enabled value for {tool_name}: {enabled}")

    # Check rate limits (if present)
    rate_limits = profile.get("rate_limits")
    if rate_limits is not None:
        max_calls = rate_limits.get("max_calls_per_hour")
        if not isinstance(max_calls, int) or max_calls < 1:
            raise SecurityProfileError(
                f"Invalid max_calls_per_hour: {max_calls} (must be positive integer)"
            )

        for tool_name, limit in rate_limits.get("per_tool_overrides", {}).items():
            if not isinstance(limit, int) or limit < 1:
                raise SecurityProfileError(
                    f"Invalid per-tool rate limit for {tool_name}: {limit} (must be positive integer)"
                )
```

### 5. Integrate into Startup

**File:** `src/pynchy/main.py` (or wherever groups are loaded)

When loading group configs:

```python
from pynchy.config.security_defaults import get_default_profile
from pynchy.config.validation import validate_security_profile, SecurityProfileError

# ... load group config ...

# Apply default profile if not specified
if "security_profile" not in group_config or group_config["security_profile"] is None:
    group_config["security_profile"] = get_default_profile(group_name)

# Validate profile
try:
    validate_security_profile(group_config["security_profile"])
except SecurityProfileError as e:
    logger.error(f"Invalid security profile for {group_name}: {e}")
    raise
```

## Configuration Examples

### Example: God Group (Trusted)

```json
{
  "name": "Main",
  "folder": "main",
  "security_profile": {
    "tools": {
      "send_message": {"tier": "read_only", "enabled": true},
      "schedule_task": {"tier": "write", "enabled": true}
    },
    "default_tier": "write",
    "allow_unknown_tools": true,
    "rate_limits": {
      "max_calls_per_hour": 500,
      "per_tool_overrides": {}
    }
  }
}
```

### Example: Banking Workspace (Strict)

```json
{
  "name": "Banking Assistant",
  "folder": "banking",
  "security_profile": {
    "tools": {
      "read_email": {"tier": "read_only", "enabled": true},
      "send_email": {"tier": "external", "enabled": true},
      "bank_balance": {"tier": "read_only", "enabled": true},
      "bank_transfer": {"tier": "external", "enabled": true}
    },
    "default_tier": "external",
    "allow_unknown_tools": false,
    "rate_limits": {
      "max_calls_per_hour": 30,
      "per_tool_overrides": {
        "bank_transfer": 5
      }
    }
  }
}
```

## Tests

**File:** `tests/test_security_profiles.py`

```python
"""Tests for security profile system."""

import pytest

from pynchy.config.security_defaults import (
    STRICT_PROFILE,
    TRUSTED_PROFILE,
    get_default_profile,
)
from pynchy.config.validation import SecurityProfileError, validate_security_profile
from pynchy.types.security import RiskTier


def test_default_profiles_valid():
    """Test that default profiles pass validation."""
    validate_security_profile(STRICT_PROFILE)
    validate_security_profile(TRUSTED_PROFILE)


def test_get_default_profile():
    """Test getting default profile for workspace."""
    main_profile = get_default_profile("main")
    assert main_profile["allow_unknown_tools"] is True

    other_profile = get_default_profile("banking")
    assert other_profile["allow_unknown_tools"] is False


def test_validate_valid_profile():
    """Test validation passes for valid profile."""
    profile = {
        "tools": {
            "send_message": {"tier": RiskTier.READ_ONLY, "enabled": True}
        },
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": True,
    }
    validate_security_profile(profile)  # Should not raise


def test_validate_invalid_tier():
    """Test validation fails for invalid tier."""
    profile = {
        "tools": {
            "bad_tool": {"tier": "invalid", "enabled": True}
        },
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": True,
    }
    with pytest.raises(SecurityProfileError, match="Invalid tier"):
        validate_security_profile(profile)


def test_validate_invalid_default_tier():
    """Test validation fails for invalid default tier."""
    profile = {
        "tools": {},
        "default_tier": "invalid",
        "allow_unknown_tools": True,
    }
    with pytest.raises(SecurityProfileError, match="Invalid default_tier"):
        validate_security_profile(profile)


def test_validate_invalid_enabled():
    """Test validation fails for non-boolean enabled."""
    profile = {
        "tools": {
            "send_message": {"tier": RiskTier.READ_ONLY, "enabled": "yes"}
        },
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": True,
    }
    with pytest.raises(SecurityProfileError, match="Invalid enabled value"):
        validate_security_profile(profile)


def test_validate_rate_limits_valid():
    """Test validation passes for valid rate limits."""
    profile = {
        "tools": {},
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": True,
        "rate_limits": {
            "max_calls_per_hour": 100,
            "per_tool_overrides": {"send_email": 10},
        },
    }
    validate_security_profile(profile)  # Should not raise


def test_validate_rate_limits_invalid_max():
    """Test validation fails for non-positive max_calls_per_hour."""
    profile = {
        "tools": {},
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": True,
        "rate_limits": {
            "max_calls_per_hour": 0,
            "per_tool_overrides": {},
        },
    }
    with pytest.raises(SecurityProfileError, match="Invalid max_calls_per_hour"):
        validate_security_profile(profile)


def test_validate_rate_limits_invalid_per_tool():
    """Test validation fails for non-positive per-tool override."""
    profile = {
        "tools": {},
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": True,
        "rate_limits": {
            "max_calls_per_hour": 100,
            "per_tool_overrides": {"send_email": -1},
        },
    }
    with pytest.raises(SecurityProfileError, match="Invalid per-tool rate limit"):
        validate_security_profile(profile)


def test_validate_rate_limits_none():
    """Test validation passes when rate_limits is None (no limits)."""
    profile = {
        "tools": {},
        "default_tier": RiskTier.WRITE,
        "allow_unknown_tools": True,
        "rate_limits": None,
    }
    validate_security_profile(profile)  # Should not raise
```

## Success Criteria

- [ ] Security profile types defined in `src/pynchy/types/security.py` (including `RateLimitConfig`)
- [ ] Group config schema updated to include `security_profile`
- [ ] Default profiles created (strict and trusted) with sensible rate limit defaults
- [ ] Validation logic implemented and tested (including rate limit validation)
- [ ] Startup integration validates profiles and applies defaults
- [ ] Tests pass (validation, defaults, rate limits, error cases)
- [ ] Documentation updated with examples

## Documentation

Update the following:

1. **Group configuration docs** - Add security_profile field explanation
2. **Security model docs** - Explain risk tiers and the lethal trifecta
3. **Examples** - Show trusted vs strict profiles

## Next Steps

After this is complete:
- Step 2: Implement MCP tools with basic policy checking
- Step 3+: Add service-specific integrations (email, calendar, passwords)

## References

- [The Lethal Trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) — Simon Willison
- [AI Agent Security](https://simonwillison.net/2025/Jun/15/ai-agent-security/) — Simon Willison
- [Meta: Practical AI Agent Security](https://ai.meta.com/blog/practical-ai-agent-security/) — Agents Rule of Two
