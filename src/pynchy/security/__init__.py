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
