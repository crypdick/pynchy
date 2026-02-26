"""Session-scoped security enforcement for all tool calls.

One SecurityGate per container invocation, shared by IPC and MCP callers.
Wraps SecurityPolicy to provide sticky taint tracking and a uniform
evaluate interface.

Registry keyed by (group_folder, invocation_ts) to support future
concurrent containers for the same group.
"""

from __future__ import annotations

from pynchy.security.middleware import PolicyDecision, SecurityPolicy
from pynchy.types import WorkspaceSecurity

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


def destroy_gate(source_group: str, invocation_ts: float) -> None:
    """Remove a SecurityGate when its container exits."""
    _gates.pop((source_group, invocation_ts), None)
