"""Policy enforcement for MCP tool calls.

Evaluates IPC requests against workspace security profiles, applying
rate limiting and tier-based access control. Security audit events are
stored in the existing messages table.
"""

from pynchy.security.audit import prune_security_audit, record_security_event
from pynchy.security.middleware import (
    PolicyDecision,
    PolicyDeniedError,
    PolicyMiddleware,
)

__all__ = [
    "PolicyDecision",
    "PolicyDeniedError",
    "PolicyMiddleware",
    "prune_security_audit",
    "record_security_event",
]
