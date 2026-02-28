"""Policy enforcement for MCP tool calls.

Evaluates IPC requests against service trust declarations using
two-taint tracking (corruption + secret). Security audit events
are stored in the existing messages table.  The approval submodule
(pynchy.host.container_manager.security.approval) provides a file-backed
state machine for human-in-the-loop gating — import directly to avoid
circular imports.
"""

from pynchy.host.container_manager.security.audit import prune_security_audit, record_security_event
from pynchy.host.container_manager.security.middleware import (
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
