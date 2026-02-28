"""Session-scoped security enforcement for all tool calls.

One SecurityGate per container invocation, shared by IPC and MCP callers.
Wraps SecurityPolicy to provide sticky taint tracking and a uniform
evaluate interface.

Registry keyed by (group_folder, invocation_ts) to support future
concurrent containers for the same group.
"""

from __future__ import annotations

from pynchy.host.container_manager.security.middleware import PolicyDecision, SecurityPolicy
from pynchy.types import ServiceTrustConfig, WorkspaceSecurity

# ---------------------------------------------------------------------------
# SecurityGate
# ---------------------------------------------------------------------------


class SecurityGate:
    """Session-scoped security enforcement for all tool calls."""

    def __init__(self, security: WorkspaceSecurity) -> None:
        self._policy = SecurityPolicy(security)

    @property
    def policy(self) -> SecurityPolicy:
        """Access the underlying SecurityPolicy (for taint inspection)."""
        return self._policy

    def evaluate_read(self, service: str) -> PolicyDecision:
        """Evaluate a read operation. Delegates to SecurityPolicy."""
        return self._policy.evaluate_read(service)

    def evaluate_write(self, service: str, data: dict) -> PolicyDecision:
        """Evaluate a write operation. Delegates to SecurityPolicy."""
        return self._policy.evaluate_write(service, data)

    def notify_file_access(self) -> None:
        """Forward file-access notification to the policy."""
        self._policy.notify_file_access()


# ---------------------------------------------------------------------------
# Registry -- keyed by (group_folder, invocation_ts)
# ---------------------------------------------------------------------------

_gates: dict[tuple[str, float], SecurityGate] = {}


def create_gate(
    source_group: str,
    invocation_ts: float,
    security: WorkspaceSecurity,
) -> SecurityGate:
    """Create and register a SecurityGate for a container invocation."""
    gate = SecurityGate(security)
    _gates[(source_group, invocation_ts)] = gate
    return gate


def get_gate(source_group: str, invocation_ts: float) -> SecurityGate | None:
    """Look up a SecurityGate by group and invocation timestamp."""
    return _gates.get((source_group, invocation_ts))


def get_gate_for_group(source_group: str) -> SecurityGate | None:
    """Look up a SecurityGate by group folder only (returns the latest).

    Used by IPC handlers that don't have the invocation_ts.
    When multiple gates exist for the same group (concurrent containers),
    returns the one with the highest timestamp.
    """
    matches = [(ts, g) for (grp, ts), g in _gates.items() if grp == source_group]
    if not matches:
        return None
    return max(matches, key=lambda x: x[0])[1]


def destroy_gate(source_group: str, invocation_ts: float) -> None:
    """Remove a SecurityGate when its container exits."""
    _gates.pop((source_group, invocation_ts), None)


# ---------------------------------------------------------------------------
# Security resolution -- shared by IPC handler and future MCP proxy
# ---------------------------------------------------------------------------


def resolve_security(source_group: str, *, is_admin: bool = False) -> WorkspaceSecurity:
    """Resolve the security profile for a workspace from config.toml.

    Merges top-level [services.*] declarations with per-workspace overrides.
    Falls back to maximally cautious defaults (all True) if unconfigured.

    Admin workspaces get an empty services dict (no gating) since they
    are fully trusted.
    """
    if is_admin:
        # Admin: all services default to ServiceTrustConfig() which is
        # maximally cautious. But admin workspaces skip policy gates
        # at the handler level, so this is fine.
        return WorkspaceSecurity()

    from pynchy.config import get_settings

    s = get_settings()
    ws_config = s.workspaces.get(source_group)

    if ws_config is None or ws_config.security is None:
        return WorkspaceSecurity()

    sec = ws_config.security

    # Build per-service trust configs from TOML
    services: dict[str, ServiceTrustConfig] = {}
    for svc_name, svc_cfg in sec.services.items():
        services[svc_name] = ServiceTrustConfig(
            public_source=svc_cfg.public_source,
            secret_data=svc_cfg.secret_data,
            public_sink=svc_cfg.public_sink,
            dangerous_writes=svc_cfg.dangerous_writes,
        )

    return WorkspaceSecurity(
        services=services,
        contains_secrets=sec.contains_secrets,
    )
