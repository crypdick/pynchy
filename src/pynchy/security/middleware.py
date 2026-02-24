"""Trust-based policy engine for the lethal trifecta defense.

Evaluates service operations against per-service trust declarations
and two independent taint flags (corruption + secret). Derives gating
decisions from the combination — users configure four booleans per
service, not risk tiers.

See docs/plans/2026-02-23-lethal-trifecta-defenses-design.md for the
full gating matrix and design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass

from pynchy.security.secrets_scanner import scan_payload_for_secrets
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

        - forbidden -> blocked
        - public_source=True -> deputy scan, corruption taint set
        - public_source=False -> no gating
        - secret_data=True -> secret taint set (always, on any read)
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

        # dangerous_writes=True -> always needs human confirmation
        if trust.dangerous_writes:
            needs_human = True

        # Full trifecta: corruption + secret + public_sink
        if self._corruption_tainted and self._secret_tainted and trust.public_sink:
            needs_human = True

        # Payload secrets scan — escalate if secrets detected
        scan_result = scan_payload_for_secrets(data)
        if scan_result.secrets_found:
            needs_human = True

        reason_parts = []
        if needs_deputy:
            reason_parts.append("deputy (corruption taint)")
        if needs_human:
            reason_parts.append("human confirmation")
        if scan_result.secrets_found:
            reason_parts.append(f"secrets detected in payload ({', '.join(scan_result.detected)})")
        reason = "; ".join(reason_parts) if reason_parts else None

        return PolicyDecision(
            allowed=True,
            reason=reason,
            needs_deputy=needs_deputy,
            needs_human=needs_human,
        )
