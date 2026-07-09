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
from typing import Any

from pynchy.host.container_manager.security.secrets_scanner import scan_payload_for_secrets
from pynchy.types import CapabilityRule, ServiceTrustConfig, WorkspaceSecurity

# Default trust for unknown services — maximally cautious
_UNKNOWN_SERVICE = ServiceTrustConfig()


class PolicyDeniedError(Exception):
    """Raised when policy denies a request. Non-retryable."""


@dataclass
class PolicyDecision:
    """Result of policy evaluation."""

    allowed: bool
    reason: str | None = None
    needs_cop: bool = False
    needs_human: bool = False


class SecurityPolicy:
    """Single entry point for all security decisions per container invocation.

    Instantiated once per container run. Taint state is sticky for the
    lifetime of the invocation — cleared only when the container restarts.
    """

    def __init__(self, security: WorkspaceSecurity) -> None:
        self._services = security.services
        self._capabilities = security.capabilities
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
        - public_source=True -> cop scan, corruption taint set
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
                reason=f"Public source '{service}': cop scan required",
                needs_cop=True,
            )

        return PolicyDecision(allowed=True)

    def evaluate_capability(self, capability: str) -> PolicyDecision:
        """Evaluate an explicit semantic capability rule.

        Capability IDs use dotted segments and support trailing ``.*``
        wildcards, for example ``mcp.email.send`` and ``mcp.email.*``.
        Missing rules are neutral: the service-trust policy still applies.
        """
        rule = self._matching_capability_rule(capability)
        if rule is None or rule.decision == "allow":
            return PolicyDecision(allowed=True)
        if rule.decision == "deny":
            return PolicyDecision(
                allowed=False,
                reason=f"Capability '{capability}' denied by policy",
            )
        return PolicyDecision(
            allowed=True,
            reason=f"Capability '{capability}' requires human approval",
            needs_human=True,
        )

    def evaluate_write(self, service: str, data: dict[str, Any]) -> PolicyDecision:
        """Evaluate a write operation on a service.

        Checks forbidden first, then derives gating from the matrix:
        - Cop: corruption_tainted (any write by potentially-hijacked agent)
        - Human: dangerous_writes=True OR (corruption + secret + public_sink)
        """
        trust = self._get_trust(service)
        forbidden = self._forbidden_write_decision(service, trust)
        if forbidden is not None:
            return forbidden

        scan_result = scan_payload_for_secrets(data)
        needs_cop, needs_human = self._write_gate_flags(
            trust,
            payload_has_secrets=scan_result.secrets_found,
        )
        reason = self._write_reason(
            needs_cop=needs_cop,
            needs_human=needs_human,
            detected_secrets=scan_result.detected,
        )

        return PolicyDecision(
            allowed=True,
            reason=reason,
            needs_cop=needs_cop,
            needs_human=needs_human,
        )

    def _matching_capability_rule(self, capability: str) -> CapabilityRule | None:
        for candidate in _capability_candidates(capability):
            if candidate in self._capabilities:
                return self._capabilities[candidate]
        return None

    def _forbidden_write_decision(
        self,
        service: str,
        trust: ServiceTrustConfig,
    ) -> PolicyDecision | None:
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
        return None

    def _write_gate_flags(
        self,
        trust: ServiceTrustConfig,
        *,
        payload_has_secrets: bool,
    ) -> tuple[bool, bool]:
        needs_cop = self._corruption_tainted
        needs_human = bool(trust.dangerous_writes)

        if self._corruption_tainted and self._secret_tainted and trust.public_sink:
            needs_human = True
        if payload_has_secrets:
            needs_human = True

        return needs_cop, needs_human

    def _write_reason(
        self,
        *,
        needs_cop: bool,
        needs_human: bool,
        detected_secrets: list[str],
    ) -> str | None:
        reason_parts = []
        if needs_cop:
            reason_parts.append("cop (corruption taint)")
        if needs_human:
            reason_parts.append("human confirmation")
        if detected_secrets:
            reason_parts.append(f"secrets detected in payload ({', '.join(detected_secrets)})")
        return "; ".join(reason_parts) if reason_parts else None


def _capability_candidates(capability: str) -> list[str]:
    parts = [part for part in capability.split(".") if part]
    if not parts:
        return []

    candidates = [".".join(parts)]
    candidates.extend(".".join([*parts[:index], "*"]) for index in range(len(parts) - 1, 0, -1))
    return candidates
