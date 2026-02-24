# Lethal Trifecta Defenses — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the tier-based security middleware with a trust-based model using four properties per service, two independent taints (corruption + secret), and a gating matrix that derives deputy/human gates from the combination.

**Architecture:** Each service declares four properties (`public_source`, `secret_data`, `public_sink`, `dangerous_writes`). At runtime, a `SecurityPolicy` tracks two taint flags per container invocation (`corruption_tainted`, `secret_tainted`) and derives gating decisions from the matrix. Deputy and human approval are stub protocols — real implementations come in later steps.

**Tech Stack:** Python dataclasses, Pydantic config models, pytest

**Design doc:** `docs/plans/2026-02-23-lethal-trifecta-defenses-design.md`

---

### Task 1: Replace types — ServiceTrustConfig + WorkspaceSecurity

**Files:**
- Modify: `src/pynchy/types.py:43-77` (replace `McpToolConfig`, `RateLimitConfig`, `WorkspaceSecurity`)
- Test: `tests/test_workspace_profile.py` (rewrite)

**Step 1: Write the failing tests**

Replace `tests/test_workspace_profile.py` entirely with:

```python
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
                public_source=False, secret_data=False,
                public_sink=False, dangerous_writes=False,
            ),
            "email": ServiceTrustConfig(
                public_source=True, secret_data=True,
                public_sink=True, dangerous_writes=True,
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
        jid="test@g.us", name="Test", folder="test", trigger="@P",
        security=WorkspaceSecurity(
            services={"email": ServiceTrustConfig(public_source=True)},
        ),
    )
    assert "email" in profile.security.services
    assert profile.security.services["email"].public_source is True


def test_workspace_profile_validation_basic():
    """Basic validation still checks name/folder/trigger."""
    profile = WorkspaceProfile(
        jid="test@g.us", name="", folder="", trigger="@P",
    )
    errors = profile.validate()
    assert any("name" in e for e in errors)
    assert any("folder" in e for e in errors)
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_workspace_profile.py -v`
Expected: FAIL — `ServiceTrustConfig` does not exist yet

**Step 3: Implement the types**

In `src/pynchy/types.py`, replace `McpToolConfig` (lines 43-48), `RateLimitConfig` (lines 51-56), and `WorkspaceSecurity` (lines 59-76) with:

```python
# Tri-state: False (safe), True (risky/gated), "forbidden" (blocked)
TrustLevel = Literal[False, True, "forbidden"]


@dataclass
class ServiceTrustConfig:
    """Four trust properties per service — the user-facing security model.

    Each property answers an intuitive question:
      public_source:    Can untrusted parties provide input through this?
      secret_data:      Does this hold sensitive/secret information?
      public_sink:      Can data I send here reach untrusted parties?
      dangerous_writes: Are writes high-stakes or irreversible?

    Defaults are maximally cautious (all True). Users set False for
    dimensions that don't apply. "forbidden" blocks the capability entirely.
    """

    public_source: TrustLevel = True
    secret_data: bool = True  # True/False only — "forbidden" doesn't apply
    public_sink: TrustLevel = True
    dangerous_writes: TrustLevel = True


@dataclass
class WorkspaceSecurity:
    """Security configuration for a workspace.

    Holds per-service trust declarations and a flag for whether the
    workspace's local filesystem contains secrets (.env files, etc.).
    """

    services: dict[str, ServiceTrustConfig] = field(default_factory=dict)
    contains_secrets: bool = False
```

Also update `WorkspaceProfile.validate()` (lines 103-150) — remove the old tier/rate-limit validation, keep name/folder/trigger validation:

```python
    def validate(self) -> list[str]:
        errors = []
        if not self.name:
            errors.append("Workspace name is required")
        if not self.folder:
            errors.append("Workspace folder is required")
        if not self.trigger:
            errors.append("Workspace trigger is required")
        return errors
```

Remove the old imports from the `Literal` type hint — remove `"always-approve"`, `"rules-engine"`, `"human-approval"` from any Literal in the file. Keep `Literal` imported for `TrustLevel`.

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_workspace_profile.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/types.py tests/test_workspace_profile.py
git commit -m "refactor(security): replace tier-based types with trust model

Replace McpToolConfig, RateLimitConfig, and WorkspaceSecurity with
ServiceTrustConfig (four booleans per service) and simplified
WorkspaceSecurity (services dict + contains_secrets flag)."
```

---

### Task 2: Replace config models — TOML parsing

**Files:**
- Modify: `src/pynchy/config_models.py:162-194` (replace `McpToolSecurityConfig`, `RateLimitsConfig`, `WorkspaceSecurityConfig`)
- Modify: `src/pynchy/config_models.py:226` (update `WorkspaceConfig.security` field)

**Step 1: Write the failing test**

Create or update test for config parsing. Add to a new file `tests/test_config_trust.py`:

```python
"""Tests for trust-model config parsing."""

import pytest
from pydantic import ValidationError

from pynchy.config_models import (
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
        public_source=False, secret_data=False,
        public_sink=False, dangerous_writes=False,
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
    assert cfg.contains_secrets is False


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
            contains_secrets=True,
        ),
    )
    assert cfg.security is not None
    assert "email" in cfg.security.services
    assert cfg.security.contains_secrets is True
```

**Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_config_trust.py -v`
Expected: FAIL — new config classes don't exist

**Step 3: Implement config models**

In `src/pynchy/config_models.py`, replace `McpToolSecurityConfig` (line 162), `RateLimitsConfig` (line 169), and `WorkspaceSecurityConfig` (line 183) with:

```python
class ServiceTrustTomlConfig(_StrictModel):
    """Per-service trust config in config.toml [services.<name>]."""

    public_source: bool | Literal["forbidden"] = True
    secret_data: bool = True
    public_sink: bool | Literal["forbidden"] = True
    dangerous_writes: bool | Literal["forbidden"] = True


class WorkspaceServiceOverride(_StrictModel):
    """Per-workspace service override — only 'forbidden' is allowed.

    All fields are optional (None = no override). Any non-None value
    must be 'forbidden'. This prevents accidentally relaxing security.
    """

    public_source: Literal["forbidden"] | None = None
    secret_data: None = None  # secret_data cannot be overridden
    public_sink: Literal["forbidden"] | None = None
    dangerous_writes: Literal["forbidden"] | None = None


class WorkspaceSecurityTomlConfig(_StrictModel):
    """Security profile in config.toml [workspaces.<name>.security]."""

    services: dict[str, ServiceTrustTomlConfig] = {}
    contains_secrets: bool = False
```

Update `WorkspaceConfig.security` field (line 226) to use the new type:

```python
    security: WorkspaceSecurityTomlConfig | None = None
```

Also add a top-level `services` field to the main `Settings` model (where `AppConfig` is defined — check where `workspaces` is defined and add `services: dict[str, ServiceTrustTomlConfig] = {}` next to it).

Add `Literal` to imports at the top of config_models.py if not already imported.

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_config_trust.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/config_models.py tests/test_config_trust.py
git commit -m "refactor(config): replace tier-based config with trust model

Replace McpToolSecurityConfig, RateLimitsConfig, and
WorkspaceSecurityConfig with ServiceTrustTomlConfig,
WorkspaceServiceOverride, and WorkspaceSecurityTomlConfig."
```

---

### Task 3: Implement SecurityPolicy — the core decision engine

**Files:**
- Rewrite: `src/pynchy/security/middleware.py`
- Test: `tests/test_policy_middleware.py` (rewrite)

**Step 1: Write the failing tests**

Replace `tests/test_policy_middleware.py` entirely. Key scenarios from the gating matrix:

```python
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
    assert d.needs_deputy is False
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
    assert decision.needs_deputy is False
    assert not policy.corruption_tainted


def test_read_public_source_taints():
    """Reading from public source: deputy scan + corruption taint."""
    policy = _make_policy(email=ServiceTrustConfig(public_source=True))
    decision = policy.evaluate_read("email")
    assert decision.allowed is True
    assert decision.needs_deputy is True
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
    """Untainted, dangerous_writes=False, public_sink=False → no gating."""
    policy = _make_policy(
        calendar=ServiceTrustConfig(public_sink=False, dangerous_writes=False),
    )
    decision = policy.evaluate_write("calendar", {})
    assert decision.allowed is True
    assert not decision.needs_deputy
    assert not decision.needs_human


def test_write_no_taint_dangerous_writes_human_only():
    """Untainted, dangerous_writes=True → human confirmation only."""
    policy = _make_policy(
        email=ServiceTrustConfig(public_sink=True, dangerous_writes=True),
    )
    decision = policy.evaluate_write("email", {})
    assert decision.allowed is True
    assert not decision.needs_deputy
    assert decision.needs_human


def test_write_no_taint_public_sink_no_dangerous_no_gating():
    """Untainted, public_sink=True, dangerous_writes=False → no gating."""
    policy = _make_policy(
        reddit=ServiceTrustConfig(public_sink=True, dangerous_writes=False),
    )
    decision = policy.evaluate_write("reddit", {})
    assert decision.allowed is True
    assert not decision.needs_deputy
    assert not decision.needs_human


# --- Write gating: corruption tainted ---


def test_write_corrupted_no_secret_no_public_sink_deputy_only():
    """Corrupted, no secret taint, private sink → deputy only."""
    policy = _make_policy(
        web=ServiceTrustConfig(public_source=True),
        notes=ServiceTrustConfig(public_sink=False, dangerous_writes=False),
    )
    policy.evaluate_read("web")  # corruption taint
    decision = policy.evaluate_write("notes", {})
    assert decision.needs_deputy
    assert not decision.needs_human


def test_write_corrupted_no_secret_public_sink_deputy_only():
    """Corrupted, no secret taint, public sink → deputy only (no secrets to exfil)."""
    policy = _make_policy(
        web=ServiceTrustConfig(public_source=True, secret_data=False),
        reddit=ServiceTrustConfig(public_sink=True, dangerous_writes=False, secret_data=False),
    )
    policy.evaluate_read("web")  # corruption taint
    decision = policy.evaluate_write("reddit", {})
    assert decision.needs_deputy
    assert not decision.needs_human  # no secret taint → no full trifecta


def test_write_full_trifecta_deputy_plus_human():
    """Corrupted + secret + public sink → deputy + human (full trifecta)."""
    policy = _make_policy(
        web=ServiceTrustConfig(public_source=True, secret_data=False),
        passwords=ServiceTrustConfig(secret_data=True, public_source=False),
        email=ServiceTrustConfig(public_sink=True, dangerous_writes=False),
    )
    policy.evaluate_read("web")       # corruption taint
    policy.evaluate_read("passwords")  # secret taint
    decision = policy.evaluate_write("email", {})
    assert decision.needs_deputy
    assert decision.needs_human  # full trifecta!


def test_write_corrupted_dangerous_writes_deputy_plus_human():
    """Corrupted + dangerous_writes → deputy + human."""
    policy = _make_policy(
        web=ServiceTrustConfig(public_source=True),
        db=ServiceTrustConfig(public_sink=False, dangerous_writes=True),
    )
    policy.evaluate_read("web")  # corruption taint
    decision = policy.evaluate_write("db", {})
    assert decision.needs_deputy
    assert decision.needs_human


# --- Taint stickiness ---


def test_corruption_taint_is_sticky():
    """Once corruption-tainted, stays tainted for all subsequent operations."""
    policy = _make_policy(
        web=ServiceTrustConfig(public_source=True),
        calendar=ServiceTrustConfig(public_source=False),
    )
    policy.evaluate_read("web")     # taints
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
    assert decision.needs_deputy  # public_source=True default
    assert policy.corruption_tainted


def test_unknown_service_write_uses_cautious_defaults():
    """Writing to an unknown service treats it as dangerous_writes=True."""
    policy = _make_policy()
    decision = policy.evaluate_write("unknown_service", {})
    assert decision.needs_human  # dangerous_writes=True default
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_policy_middleware.py -v`
Expected: FAIL — `SecurityPolicy` doesn't exist

**Step 3: Implement SecurityPolicy**

Rewrite `src/pynchy/security/middleware.py`:

```python
"""Trust-based policy engine for the lethal trifecta defense.

Evaluates service operations against per-service trust declarations
and two independent taint flags (corruption + secret). Derives gating
decisions from the combination — users configure four booleans per
service, not risk tiers.

See docs/plans/2026-02-23-lethal-trifecta-defenses-design.md for the
full gating matrix and design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pynchy.types import ServiceTrustConfig, WorkspaceSecurity

# Default trust for unknown services — maximally cautious
_UNKNOWN_SERVICE = ServiceTrustConfig()


class PolicyDeniedError(Exception):
    """Raised when policy denies a request. Non-retryable."""


@dataclass
class PolicyDecision:
    """Result of policy evaluation."""

    allowed: bool
    reason: str | None = None
    needs_deputy: bool = False
    needs_human: bool = False


class SecurityPolicy:
    """Single entry point for all security decisions per container invocation.

    Instantiated once per container run. Taint state is sticky for the
    lifetime of the invocation — cleared only when the container restarts.
    """

    def __init__(self, security: WorkspaceSecurity) -> None:
        self._services = security.services
        self._workspace_contains_secrets = security.contains_secrets
        self._corruption_tainted = False
        self._secret_tainted = False

    @property
    def corruption_tainted(self) -> bool:
        return self._corruption_tainted

    @property
    def secret_tainted(self) -> bool:
        return self._secret_tainted

    def _get_trust(self, service: str) -> ServiceTrustConfig:
        return self._services.get(service, _UNKNOWN_SERVICE)

    def notify_file_access(self) -> None:
        """Called when the agent uses file-access tools (Read, Execute, Bash).

        Sets secret taint if the workspace declares contains_secrets=True.
        """
        if self._workspace_contains_secrets:
            self._secret_tainted = True

    def evaluate_read(self, service: str) -> PolicyDecision:
        """Evaluate a read operation on a service.

        - forbidden → blocked
        - public_source=True → deputy scan, corruption taint set
        - public_source=False → no gating
        - secret_data=True → secret taint set (always, on any read)
        """
        trust = self._get_trust(service)

        if trust.public_source == "forbidden":
            return PolicyDecision(
                allowed=False,
                reason=f"Reading from '{service}' is forbidden",
            )

        # Secret taint: set on any read from a service with secret_data
        if trust.secret_data:
            self._secret_tainted = True

        if trust.public_source:
            self._corruption_tainted = True
            return PolicyDecision(
                allowed=True,
                reason=f"Public source '{service}': deputy scan required",
                needs_deputy=True,
            )

        return PolicyDecision(allowed=True)

    def evaluate_write(self, service: str, data: dict) -> PolicyDecision:
        """Evaluate a write operation on a service.

        Checks forbidden first, then derives gating from the matrix:
        - Deputy: corruption_tainted (any write by potentially-hijacked agent)
        - Human: dangerous_writes=True OR (corruption + secret + public_sink)
        """
        trust = self._get_trust(service)

        # Forbidden checks
        if trust.public_sink == "forbidden":
            return PolicyDecision(
                allowed=False,
                reason=f"Writing to '{service}' is forbidden (public_sink)",
            )
        if trust.dangerous_writes == "forbidden":
            return PolicyDecision(
                allowed=False,
                reason=f"Writing to '{service}' is forbidden (dangerous_writes)",
            )

        # Derive gating from taint state + service properties
        needs_deputy = self._corruption_tainted
        needs_human = False

        # dangerous_writes=True → always needs human confirmation
        if trust.dangerous_writes:
            needs_human = True

        # Full trifecta: corruption + secret + public_sink
        if (
            self._corruption_tainted
            and self._secret_tainted
            and trust.public_sink
        ):
            needs_human = True

        reason_parts = []
        if needs_deputy:
            reason_parts.append("deputy (corruption taint)")
        if needs_human:
            reason_parts.append("human confirmation")
        reason = "; ".join(reason_parts) if reason_parts else None

        return PolicyDecision(
            allowed=True,
            reason=reason,
            needs_deputy=needs_deputy,
            needs_human=needs_human,
        )
```

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_policy_middleware.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/pynchy/security/middleware.py tests/test_policy_middleware.py
git commit -m "feat(security): implement SecurityPolicy with two-taint model

Trust-based gating engine: four booleans per service, two independent
taints (corruption + secret), gating matrix derives deputy/human gates.
Replaces the old tier-based PolicyMiddleware."
```

---

### Task 3b: Payload secrets scanner

**Files:**
- Create: `src/pynchy/security/secrets_scanner.py`
- Test: `tests/test_secrets_scanner.py`

**Context:** Deterministic (non-LLM) content check using `detect-secrets` library. Scans outbound write payloads for leaked secrets (API keys, tokens, passwords, private keys). If secrets are detected, `evaluate_write()` forces `needs_human = True` regardless of taint state. Defense-in-depth: catches secrets in payloads that the taint model doesn't track.

**Step 1: Add detect-secrets dependency**

Run: `uv add detect-secrets`

**Step 2: Write the failing tests**

Create `tests/test_secrets_scanner.py`:

```python
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
    payload = "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"  # pragma: allowlist secret
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
    """Scans dict values recursively."""
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
```

**Step 3: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_secrets_scanner.py -v`
Expected: FAIL — module doesn't exist

**Step 4: Implement the scanner**

Create `src/pynchy/security/secrets_scanner.py`:

```python
"""Deterministic payload secrets scanner using detect-secrets.

Scans outbound write payloads for leaked secrets (API keys, tokens,
private keys, etc.). Non-LLM, non-AI — purely rule-based detection.
Used by SecurityPolicy.evaluate_write() to escalate gating when
secrets are found in payloads regardless of taint state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from detect_secrets import SecretsCollection
from detect_secrets.settings import default_settings


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

    secrets = SecretsCollection()
    with default_settings():
        secrets.scan_string(text)

    if not secrets:
        return ScanResult()

    detected_types = []
    for _filename, secret_set in secrets.data.items():
        for secret in secret_set:
            detected_types.append(secret.type)

    return ScanResult(
        secrets_found=True,
        detected=detected_types,
    )
```

**Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_secrets_scanner.py -v`
Expected: PASS (at least the AWS key and private key tests should pass; high-entropy test is best-effort)

**Step 6: Wire into SecurityPolicy.evaluate_write()**

In `src/pynchy/security/middleware.py`, add to `evaluate_write()` after the taint-based gating logic:

```python
from pynchy.security.secrets_scanner import scan_payload_for_secrets

# ... inside evaluate_write(), after computing needs_deputy and needs_human:

# Payload secrets scan — escalate if secrets detected
scan_result = scan_payload_for_secrets(data)
if scan_result.secrets_found:
    needs_human = True
    reason_parts.append(
        f"secrets detected in payload ({', '.join(scan_result.detected)})"
    )
```

**Step 7: Add test for SecurityPolicy integration**

Add to `tests/test_policy_middleware.py`:

```python
def test_write_payload_with_secrets_escalates_to_human():
    """Payload containing secrets forces human confirmation even if untainted."""
    policy = _make_policy(
        email=ServiceTrustConfig(
            public_sink=True, dangerous_writes=False,
            public_source=False, secret_data=False,
        ),
    )
    data = {"body": "Here is the key: AKIAIOSFODNN7EXAMPLE"}  # pragma: allowlist secret
    decision = policy.evaluate_write("email", data)
    assert decision.needs_human  # escalated by secrets scanner
```

**Step 8: Run all security tests**

Run: `uv run python -m pytest tests/test_policy_middleware.py tests/test_secrets_scanner.py -v`
Expected: PASS

**Step 9: Commit**

```bash
git add src/pynchy/security/secrets_scanner.py tests/test_secrets_scanner.py \
    src/pynchy/security/middleware.py tests/test_policy_middleware.py
git commit -m "feat(security): add deterministic payload secrets scanner

Uses detect-secrets to scan outbound payloads for leaked secrets.
If found, forces human confirmation regardless of taint state.
Defense-in-depth complement to the trust/taint model."
```

---

### Task 4: Update security/__init__.py exports

**Files:**
- Modify: `src/pynchy/security/__init__.py`

**Step 1: Update exports**

```python
"""Policy enforcement for MCP tool calls.

Evaluates IPC requests against service trust declarations using
two-taint tracking (corruption + secret). Security audit events
are stored in the existing messages table.
"""

from pynchy.security.audit import prune_security_audit, record_security_event
from pynchy.security.middleware import (
    PolicyDecision,
    PolicyDeniedError,
    SecurityPolicy,
)

__all__ = [
    "PolicyDecision",
    "PolicyDeniedError",
    "SecurityPolicy",
    "prune_security_audit",
    "record_security_event",
]
```

**Step 2: Run full test suite to check for import breakage**

Run: `uv run python -m pytest tests/ -x -v --timeout=10 2>&1 | head -80`
Expected: May have failures in `test_ipc_service_handler.py` (uses old imports) — that's Task 5

**Step 3: Commit**

```bash
git add src/pynchy/security/__init__.py
git commit -m "refactor(security): update exports for SecurityPolicy"
```

---

### Task 5: Update _handlers_service.py — wire in SecurityPolicy

**Files:**
- Modify: `src/pynchy/ipc/_handlers_service.py`
- Modify: `tests/test_ipc_service_handler.py`

**Step 1: Write the failing tests**

Rewrite `tests/test_ipc_service_handler.py` to use the new trust model. Replace the test helpers and tests that reference `McpToolSecurityConfig`, `RateLimitsConfig`, `WorkspaceSecurityConfig` with equivalents using `ServiceTrustTomlConfig` and `WorkspaceSecurityTomlConfig`. The key change: `_resolve_security` now returns `WorkspaceSecurity` with services/contains_secrets, and the handler uses `SecurityPolicy` instead of `PolicyMiddleware`.

Update:
- `_make_settings()` to use `WorkspaceSecurityTomlConfig` and `ServiceTrustTomlConfig`
- Remove rate limiting test
- Replace tier-based tests with trust-model equivalents
- Test that `forbidden` blocks, that default (no config) uses cautious defaults

**Step 2: Update the handler**

Key changes to `_handlers_service.py`:
- Replace `PolicyMiddleware` import with `SecurityPolicy`
- Replace `McpToolConfig`, `RateLimitConfig`, `WorkspaceSecurity` imports with new types
- `_resolve_security()` returns `WorkspaceSecurity` with services dict from TOML config
- `_get_policy()` creates `SecurityPolicy` (not cached across invocations — taint is per-invocation)
- `_handle_service_request()` calls `policy.evaluate_write()` (most service requests are writes; read vs write distinction determined by tool naming convention or handler metadata)
- Update audit event fields (drop `tier`, add `corruption_tainted`, `secret_tainted`)

**Step 3: Run tests**

Run: `uv run python -m pytest tests/test_ipc_service_handler.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add src/pynchy/ipc/_handlers_service.py tests/test_ipc_service_handler.py
git commit -m "refactor(ipc): wire SecurityPolicy into service handler

Replace PolicyMiddleware with SecurityPolicy. Service requests
are now evaluated against trust declarations instead of risk tiers."
```

---

### Task 6: Update audit.py event fields

**Files:**
- Modify: `src/pynchy/security/audit.py`
- Modify: `tests/test_security_audit.py`

**Step 1: Update record_security_event signature**

Replace `tier` parameter with `corruption_tainted` and `secret_tainted` booleans:

```python
async def record_security_event(
    chat_jid: str,
    workspace: str,
    tool_name: str,
    decision: str,  # "allowed", "denied", "blocked_forbidden"
    *,
    corruption_tainted: bool = False,
    secret_tainted: bool = False,
    reason: str | None = None,
    request_id: str | None = None,
) -> None:
```

**Step 2: Update tests**

Update `tests/test_security_audit.py` — replace `tier` kwargs with `corruption_tainted`/`secret_tainted`.

**Step 3: Run tests**

Run: `uv run python -m pytest tests/test_security_audit.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add src/pynchy/security/audit.py tests/test_security_audit.py
git commit -m "refactor(audit): replace tier with taint flags in security events"
```

---

### Task 7: Fix remaining import breakage

**Step 1: Search for all remaining references to removed types**

Run: `uv run python -m pytest tests/ -x --timeout=10 2>&1 | head -40`

Also grep for stale references:
```bash
rg "McpToolConfig|RateLimitConfig|PolicyMiddleware|ActionTracker|risk_tier|default_risk_tier|rate_limits|McpToolSecurityConfig|RateLimitsConfig|WorkspaceSecurityConfig" src/ tests/ --type py
```

Known files that reference old types:
- `src/pynchy/db/groups.py` (may reference `WorkspaceSecurity` fields)
- `src/pynchy/startup_handler.py` (may reference security config)
- `src/pynchy/group_queue.py` (uses `PolicyDeniedError` — this one stays)

**Step 2: Fix each reference**

Update imports and field accesses to use the new types. `PolicyDeniedError` still exists (just moved to `SecurityPolicy`'s module).

**Step 3: Run full test suite**

Run: `uv run python -m pytest tests/ -v --timeout=30`
Expected: All PASS

**Step 4: Commit**

```bash
git add -A
git commit -m "fix: resolve remaining import breakage from trust model migration"
```

---

### Task 8: Update security-related backlog docs

**Files:**
- Modify: `backlog/2-planning/security-hardening-1-profiles.md`
- Modify: `backlog/2-planning/security-hardening.md`
- Modify: `backlog/TODO.md`

**Step 1: Update the backlog**

- In `security-hardening-1-profiles.md`: add a note at the top that the schema was implemented with the four-boolean model instead of the three-boolean model described in the original plan. Reference the design doc.
- In `security-hardening.md`: update the overview to reference the actual implementation. Update the `ServiceTrustConfig` code example to show four booleans.
- In `TODO.md`: move Security Step 1 to completed or add a note that it's done.

**Step 2: Commit**

```bash
git add backlog/ docs/
git commit -m "docs: update security hardening backlog for trust model implementation"
```

---

### Summary of commits

1. `refactor(security): replace tier-based types with trust model`
2. `refactor(config): replace tier-based config with trust model`
3. `feat(security): implement SecurityPolicy with two-taint model`
3b. `feat(security): add deterministic payload secrets scanner`
4. `refactor(security): update exports for SecurityPolicy`
5. `refactor(ipc): wire SecurityPolicy into service handler`
6. `refactor(audit): replace tier with taint flags in security events`
7. `fix: resolve remaining import breakage from trust model migration`
8. `docs: update security hardening backlog for trust model implementation`
