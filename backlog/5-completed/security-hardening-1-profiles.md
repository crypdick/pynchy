# Security Hardening: Step 1 - Workspace Security Profiles

> **Status: IMPLEMENTED** (2026-02-24) — Schema was implemented with a **four-boolean model** (`public_source`, `secret_data`, `public_sink`, `dangerous_writes`) plus a tri-state (`"forbidden"`) instead of the three-boolean model described below. Also uses two independent taint flags (corruption + secret) instead of a single taint. See `docs/plans/2026-02-23-lethal-trifecta-defenses-design.md` for the final design.

## Overview

Establish the security profile schema and configuration system that defines how each workspace interacts with external services, based on a three-boolean trust model per service.

## Scope

This step creates the foundational security configuration layer without implementing any actual service integrations. It's purely about defining the security model and making it configurable per workspace.

## Dependencies

- ✅ Current workspace/group system (already implemented)
- ✅ IPC MCP system (already implemented)

## Background: The Lethal Trifecta

The orchestrator (agent) has access to services that may provide:
- **A) Untrusted input** — data from sources we don't control (emails from strangers, web content)
- **B) Sensitive data** — information that could cause harm if leaked (passwords, banking info)
- **C) Untrusted sinks** — channels that could be used for exfiltration or harm (sending emails, external APIs)

Having all three is dangerous. But **not every service contributes to the trifecta**. A personal calendar is fully trusted. Email has untrusted input and is an untrusted sink. Passwords contain sensitive data. The gating applied to each service should be **derived from the trust model**, not manually assigned.

## Service Trust Declarations

Each service declares which legs of the trifecta it contributes:

```python
class ServiceTrustConfig(BaseModel):
    trusted_source: bool = False   # is data from this service trusted?
    sensitive_info: bool = True    # does this service expose sensitive data?
    trusted_sink: bool = False     # is writing to this service safe?
```

Defaults are maximally restrictive (untrusted source, sensitive, untrusted sink). Unknown services always get these defaults.

Examples:

| Service | trusted_source | sensitive_info | trusted_sink | Gating |
|---------|---------------|----------------|-------------|--------|
| Personal calendar | true | false | true | **None** (fully trusted) |
| Email (IMAP/SMTP) | false | false | false | Deputy scan on reads, human gate on sends |
| Password manager | true | true | false | Human gate on retrieval |
| Web browsing | false | false | N/A | Deputy scan on content |

## Derived Policy and Taint Tracking

The policy middleware **composes** trust declarations across all services the workspace has access to. Taint propagation is the core mechanism:

| Condition | Action |
|-----------|--------|
| Container reads from service with `trusted_source = false` | Deputy sanitizes content; container is marked **tainted** |
| Tainted container accesses service with `sensitive_info = true` | Deputy + human gate triggers |
| Tainted container writes to service with `trusted_sink = false` | Deputy + human gate triggers |
| All services fully trusted | No gating — execute unfettered |
| Unknown service (not declared in profile) | Defaults to `{trusted_source: false, sensitive_info: true, trusted_sink: false}` |

Taint is sticky for the lifetime of a container invocation. Once tainted (by reading untrusted input), any subsequent access to sensitive data or untrusted sinks requires gating. This prevents indirect exfiltration: an attacker-controlled email cannot silently cause the agent to read passwords and send them out.

Rate limiting applies regardless of trust declarations or taint state.

## Implementation

### 1. Define Security Profile Schema

**File:** `src/pynchy/types/security.py` (new file)

```python
"""Security profile types for workspace isolation."""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel


class ServiceTrustConfig(BaseModel):
    """Trust declaration for a service — maps to the lethal trifecta legs.

    Each service declares which legs of the trifecta it contributes.
    The policy engine composes these across all services to determine gating.
    Unknown services default to maximally restrictive:
    {trusted_source: False, sensitive_info: True, trusted_sink: False}
    """

    trusted_source: bool = False   # is data from this service trusted? (false = deputy scan + taint)
    sensitive_info: bool = True    # does this service expose sensitive data? (true = gated if tainted)
    trusted_sink: bool = False     # is writing to this service safe? (false = gated if tainted)


class RateLimitConfig(TypedDict):
    """Rate limiting configuration for a workspace."""

    max_calls_per_hour: int  # Global limit across all services
    per_service_overrides: dict[str, int]  # service_name -> max_calls_per_hour


class WorkspaceSecurityProfile(TypedDict):
    """Security configuration for a workspace.

    The profile declares trust levels for each service. Services not listed
    are treated as unknown and get maximally restrictive defaults.
    """

    services: dict[str, ServiceTrustConfig]  # service_name -> trust declaration
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

from pynchy.types.security import ServiceTrustConfig, WorkspaceSecurityProfile

# Unknown service default — maximally restrictive
UNKNOWN_SERVICE_TRUST = ServiceTrustConfig()
# Equivalent to: ServiceTrustConfig(trusted_source=False, sensitive_info=True, trusted_sink=False)

# Conservative default: no services declared, everything unknown
STRICT_PROFILE: WorkspaceSecurityProfile = {
    "services": {},
    "rate_limits": {
        "max_calls_per_hour": 60,
        "per_service_overrides": {},
    },
}

# Permissive profile for trusted workspaces (like admin group)
TRUSTED_PROFILE: WorkspaceSecurityProfile = {
    "services": {
        "calendar": ServiceTrustConfig(
            trusted_source=True, sensitive_info=False, trusted_sink=True,
        ),
        "email": ServiceTrustConfig(
            trusted_source=False, sensitive_info=False, trusted_sink=False,
        ),
        "passwords": ServiceTrustConfig(
            trusted_source=True, sensitive_info=True, trusted_sink=False,
        ),
    },
    "rate_limits": {
        "max_calls_per_hour": 500,
        "per_service_overrides": {},
    },
}


def get_default_profile(workspace_name: str) -> WorkspaceSecurityProfile:
    """Return appropriate default profile for workspace."""
    if workspace_name == "main":
        return TRUSTED_PROFILE
    return STRICT_PROFILE


def get_service_trust(
    profile: WorkspaceSecurityProfile, service_name: str
) -> ServiceTrustConfig:
    """Get trust config for a service, defaulting to maximally restrictive for unknown services."""
    return profile["services"].get(service_name, UNKNOWN_SERVICE_TRUST)
```

### 4. Add Profile Validation

**File:** `src/pynchy/config/validation.py` (new or extend existing)

```python
"""Validate security profiles on startup."""

from pynchy.types.security import ServiceTrustConfig, WorkspaceSecurityProfile


class SecurityProfileError(Exception):
    """Raised when security profile is invalid."""


def validate_security_profile(profile: WorkspaceSecurityProfile) -> None:
    """Validate a security profile configuration.

    Raises:
        SecurityProfileError: If profile is invalid
    """
    # Check all service trust configs are valid ServiceTrustConfig instances
    for service_name, trust_config in profile["services"].items():
        if not isinstance(trust_config, ServiceTrustConfig):
            raise SecurityProfileError(
                f"Invalid trust config for service '{service_name}': "
                f"expected ServiceTrustConfig, got {type(trust_config).__name__}"
            )
        for field in ("trusted_source", "sensitive_info", "trusted_sink"):
            value = getattr(trust_config, field)
            if not isinstance(value, bool):
                raise SecurityProfileError(
                    f"Invalid {field} for service '{service_name}': "
                    f"expected bool, got {type(value).__name__}"
                )

    # Check rate limits (if present)
    rate_limits = profile.get("rate_limits")
    if rate_limits is not None:
        max_calls = rate_limits.get("max_calls_per_hour")
        if not isinstance(max_calls, int) or max_calls < 1:
            raise SecurityProfileError(
                f"Invalid max_calls_per_hour: {max_calls} (must be positive integer)"
            )

        for service_name, limit in rate_limits.get("per_service_overrides", {}).items():
            if not isinstance(limit, int) or limit < 1:
                raise SecurityProfileError(
                    f"Invalid per-service rate limit for {service_name}: "
                    f"{limit} (must be positive integer)"
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

### Example: God Group (Trusted Calendar + Email)

```toml
[workspaces.main.security.services.calendar]
trusted_source = true
sensitive_info = false
trusted_sink = true

[workspaces.main.security.services.email]
trusted_source = false
sensitive_info = false
trusted_sink = false

[workspaces.main.security.rate_limits]
max_calls_per_hour = 500
```

With this config:
- Calendar tools execute unfettered (all flags trusted, never taints)
- Email reads go through deputy scan and **taint** the container (`trusted_source = false`)
- Once tainted, email sends require human approval (`trusted_sink = false`)
- If the agent only uses calendar (no email reads), no gating at all

### Example: Banking Workspace (Strict)

```toml
[workspaces.banking.security.services.banking]
trusted_source = true
sensitive_info = true
trusted_sink = false

[workspaces.banking.security.services.email]
trusted_source = false
sensitive_info = false
trusted_sink = false

[workspaces.banking.security.rate_limits]
max_calls_per_hour = 30

[workspaces.banking.security.rate_limits.per_service_overrides]
banking = 5
```

With this config:
- Banking data is sensitive (`sensitive_info = true`); if the container is tainted, accessing banking requires gating
- Email reading taints the container (`trusted_source = false`)
- Tainted container + banking access (`sensitive_info = true`) = deputy + human gate
- Tainted container + email send (`trusted_sink = false`) = deputy + human gate
- If the agent only accesses banking (no email reads), banking reads are ungated (source is trusted, not tainted)
- Banking limited to 5 calls/hour even if approved

### Example: Research Workspace (Web + Notes)

```toml
[workspaces.research.security.services.web]
trusted_source = false
sensitive_info = false
trusted_sink = false

[workspaces.research.security.services.notes]
trusted_source = true
sensitive_info = false
trusted_sink = true

[workspaces.research.security.rate_limits]
max_calls_per_hour = 200
```

With this config:
- Web browsing taints the container immediately
- Notes are fully trusted, but once tainted from web content, writes to notes still execute (notes is a trusted sink)
- No sensitive data in this workspace, so taint only gates untrusted sinks
- Unknown services get maximally restrictive defaults

## Tests

**File:** `tests/test_security_profiles.py`

```python
"""Tests for security profile system."""

import pytest

from pynchy.config.security_defaults import (
    STRICT_PROFILE,
    TRUSTED_PROFILE,
    UNKNOWN_SERVICE_TRUST,
    get_default_profile,
    get_service_trust,
)
from pynchy.config.validation import SecurityProfileError, validate_security_profile
from pynchy.types.security import ServiceTrustConfig


# --- ServiceTrustConfig defaults ---


def test_unknown_service_defaults_maximally_restrictive():
    """Unknown services default to untrusted source, sensitive, untrusted sink."""
    trust = UNKNOWN_SERVICE_TRUST
    assert trust.trusted_source is False
    assert trust.sensitive_info is True
    assert trust.trusted_sink is False


def test_service_trust_config_default_constructor():
    """Default constructor matches unknown-service defaults."""
    trust = ServiceTrustConfig()
    assert trust.trusted_source is False
    assert trust.sensitive_info is True
    assert trust.trusted_sink is False


# --- Default profiles ---


def test_default_profiles_valid():
    """Default profiles pass validation."""
    validate_security_profile(STRICT_PROFILE)
    validate_security_profile(TRUSTED_PROFILE)


def test_get_default_profile_main():
    """Main workspace gets trusted profile."""
    profile = get_default_profile("main")
    assert "calendar" in profile["services"]
    assert "email" in profile["services"]
    assert "passwords" in profile["services"]


def test_get_default_profile_other():
    """Non-main workspaces get strict profile (no services declared)."""
    profile = get_default_profile("banking")
    assert profile["services"] == {}


def test_strict_profile_has_rate_limits():
    """Strict profile has conservative rate limits."""
    assert STRICT_PROFILE["rate_limits"] is not None
    assert STRICT_PROFILE["rate_limits"]["max_calls_per_hour"] == 60


def test_trusted_profile_services():
    """Trusted profile declares calendar, email, passwords with correct trust."""
    services = TRUSTED_PROFILE["services"]

    # Calendar is fully trusted
    assert services["calendar"].trusted_source is True
    assert services["calendar"].sensitive_info is False
    assert services["calendar"].trusted_sink is True

    # Email is untrusted source and untrusted sink
    assert services["email"].trusted_source is False
    assert services["email"].sensitive_info is False
    assert services["email"].trusted_sink is False

    # Passwords are trusted source but sensitive
    assert services["passwords"].trusted_source is True
    assert services["passwords"].sensitive_info is True
    assert services["passwords"].trusted_sink is False


# --- get_service_trust ---


def test_get_service_trust_known_service():
    """Known services return their declared trust config."""
    trust = get_service_trust(TRUSTED_PROFILE, "calendar")
    assert trust.trusted_source is True
    assert trust.sensitive_info is False
    assert trust.trusted_sink is True


def test_get_service_trust_unknown_service():
    """Unknown services return maximally restrictive defaults."""
    trust = get_service_trust(TRUSTED_PROFILE, "unknown_service")
    assert trust.trusted_source is False
    assert trust.sensitive_info is True
    assert trust.trusted_sink is False


# --- Taint tracking scenarios ---


def test_fully_trusted_service_does_not_taint():
    """A fully trusted service (all booleans true/false-safe) should not cause taint."""
    trust = ServiceTrustConfig(trusted_source=True, sensitive_info=False, trusted_sink=True)
    # trusted_source = True means reading from this service does not taint
    assert trust.trusted_source is True


def test_untrusted_source_causes_taint():
    """Reading from an untrusted source should taint the container."""
    trust = ServiceTrustConfig(trusted_source=False, sensitive_info=False, trusted_sink=True)
    # trusted_source = False means reading taints
    assert trust.trusted_source is False


def test_tainted_access_to_sensitive_requires_gating():
    """A tainted container accessing sensitive data should require gating."""
    email = ServiceTrustConfig(trusted_source=False, sensitive_info=False, trusted_sink=False)
    passwords = ServiceTrustConfig(trusted_source=True, sensitive_info=True, trusted_sink=False)
    # If container reads email (trusted_source=False), it becomes tainted
    # Then accessing passwords (sensitive_info=True) requires gating
    assert email.trusted_source is False  # taints
    assert passwords.sensitive_info is True  # gated when tainted


def test_tainted_write_to_untrusted_sink_requires_gating():
    """A tainted container writing to an untrusted sink should require gating."""
    web = ServiceTrustConfig(trusted_source=False, sensitive_info=False, trusted_sink=False)
    email = ServiceTrustConfig(trusted_source=False, sensitive_info=False, trusted_sink=False)
    # Read web (taints), then send email (untrusted sink) = gated
    assert web.trusted_source is False  # taints
    assert email.trusted_sink is False  # gated when tainted


def test_tainted_write_to_trusted_sink_no_gating():
    """A tainted container writing to a trusted sink should NOT require gating."""
    web = ServiceTrustConfig(trusted_source=False, sensitive_info=False, trusted_sink=False)
    notes = ServiceTrustConfig(trusted_source=True, sensitive_info=False, trusted_sink=True)
    # Read web (taints), write notes (trusted sink) = no gating
    assert web.trusted_source is False  # taints
    assert notes.trusted_sink is True  # safe even when tainted


# --- Validation ---


def test_validate_valid_profile():
    """Validation passes for valid profile with services."""
    profile = {
        "services": {
            "calendar": ServiceTrustConfig(
                trusted_source=True, sensitive_info=False, trusted_sink=True,
            ),
        },
        "rate_limits": None,
    }
    validate_security_profile(profile)  # Should not raise


def test_validate_empty_services():
    """Validation passes for profile with no services (strict)."""
    profile = {
        "services": {},
        "rate_limits": None,
    }
    validate_security_profile(profile)  # Should not raise


def test_validate_invalid_trust_config_type():
    """Validation fails when service has wrong type instead of ServiceTrustConfig."""
    profile = {
        "services": {
            "bad_service": {"trusted_source": True},  # dict, not ServiceTrustConfig
        },
        "rate_limits": None,
    }
    with pytest.raises(SecurityProfileError, match="Invalid trust config"):
        validate_security_profile(profile)


def test_validate_rate_limits_valid():
    """Validation passes for valid rate limits."""
    profile = {
        "services": {},
        "rate_limits": {
            "max_calls_per_hour": 100,
            "per_service_overrides": {"email": 10},
        },
    }
    validate_security_profile(profile)  # Should not raise


def test_validate_rate_limits_invalid_max():
    """Validation fails for non-positive max_calls_per_hour."""
    profile = {
        "services": {},
        "rate_limits": {
            "max_calls_per_hour": 0,
            "per_service_overrides": {},
        },
    }
    with pytest.raises(SecurityProfileError, match="Invalid max_calls_per_hour"):
        validate_security_profile(profile)


def test_validate_rate_limits_invalid_per_service():
    """Validation fails for non-positive per-service override."""
    profile = {
        "services": {},
        "rate_limits": {
            "max_calls_per_hour": 100,
            "per_service_overrides": {"email": -1},
        },
    }
    with pytest.raises(SecurityProfileError, match="Invalid per-service rate limit"):
        validate_security_profile(profile)


def test_validate_rate_limits_none():
    """Validation passes when rate_limits is None (no limits)."""
    profile = {
        "services": {},
        "rate_limits": None,
    }
    validate_security_profile(profile)  # Should not raise
```

## Success Criteria

- [ ] Security profile types defined in `src/pynchy/types/security.py` (`ServiceTrustConfig`, `RateLimitConfig`, `WorkspaceSecurityProfile`)
- [ ] Group config schema updated to include `security_profile`
- [ ] Default profiles created (strict and trusted) with sensible rate limit defaults
- [ ] Unknown services default to maximally restrictive trust config
- [ ] `get_service_trust()` helper resolves known and unknown services
- [ ] Validation logic checks trust configs and rate limits
- [ ] Taint tracking semantics documented: untrusted source read taints container; tainted container gated on sensitive data or untrusted sink access
- [ ] Startup integration validates profiles and applies defaults
- [ ] Tests pass (trust configs, defaults, taint scenarios, validation, rate limits, error cases)

## Documentation

Update the following:

1. **Group configuration docs** - Add security_profile field explanation
2. **Security model docs** - Explain the three-boolean trust model and taint tracking
3. **Examples** - Show trusted vs strict profiles with taint propagation scenarios

## Next Steps

After this is complete:
- Step 2: Implement taint tracking in the container runtime (taint-on-read, gate-on-write)
- Step 3: Implement deputy agent scanning for untrusted source content
- Step 4+: Add service-specific integrations (email, calendar, passwords)

## References

- [The Lethal Trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) — Simon Willison
- [AI Agent Security](https://simonwillison.net/2025/Jun/15/ai-agent-security/) — Simon Willison
- [Meta: Practical AI Agent Security](https://ai.meta.com/blog/practical-ai-agent-security/) — Agents Rule of Two
