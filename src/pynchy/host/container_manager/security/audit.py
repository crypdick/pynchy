"""Security audit logging via the existing messages table.

Stores policy evaluation results as messages with sender='security'
and message_type='security_audit'. Uses the existing messages table
so no schema changes are needed.

Retention pruning is scoped to security rows only — chat history
is untouched.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pynchy.redaction import irreversibly_redact
from pynchy.secrets_scanner import scan_payload_for_secrets

_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization)\b\s*[:=]\s*\S+"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+\S+")
_URL_QUERY = re.compile(r"(https?://[^\s?]+)\?\S+")
type StoreSecurityAudit = Callable[..., Awaitable[None]]
type PruneSecurityAudit = Callable[[str, str], Awaitable[int]]

_store_security_audit: StoreSecurityAudit | None = None
_prune_security_audit: PruneSecurityAudit | None = None


def configure_security_audit_storage(
    *,
    store_security_audit: StoreSecurityAudit,
    prune_security_audit: PruneSecurityAudit,
) -> None:
    """Inject the durable audit store at host composition."""
    global _store_security_audit, _prune_security_audit  # noqa: PLW0603 - one host process owns one audit store.
    _store_security_audit = store_security_audit
    _prune_security_audit = prune_security_audit


def redact_audit_reason(reason: str | None) -> str | None:
    """Remove credential values and URL queries from persisted audit reasons."""
    if reason is None:
        return None
    locally_redacted = irreversibly_redact(reason)
    if locally_redacted != reason:
        return locally_redacted
    if scan_payload_for_secrets(reason).secrets_found:
        return "[redacted: secret-bearing reason]"
    redacted = _SENSITIVE_VALUE.sub(r"\1=<redacted>", reason)
    redacted = _BEARER_VALUE.sub("Bearer <redacted>", redacted)
    return _URL_QUERY.sub(r"\1?<redacted>", redacted)


async def record_security_event(  # noqa: PLR0913 - audit rows mirror the policy decision fields directly.
    chat_jid: str,
    workspace: str,
    tool_name: str,
    decision: str,  # "allowed", "denied", "blocked_forbidden", "approval_requested"
    *,
    corruption_tainted: bool = False,
    secret_tainted: bool = False,
    reason: str | None = None,
    request_id: str | None = None,
    capability_id: str | None = None,
    action_ids: tuple[str, ...] = (),
    rule_ids: tuple[str, ...] = (),
    guarded_action_id: str | None = None,
) -> None:
    """Record a policy evaluation in the messages table."""
    metadata = {
        "workspace": workspace,
        "tool_name": tool_name,
        "decision": decision,
        "corruption_tainted": corruption_tainted,
        "secret_tainted": secret_tainted,
        "reason": redact_audit_reason(reason),
        "request_id": request_id,
        "guarded_action_id": guarded_action_id or request_id,
        "capability_id": capability_id,
        "action_ids": action_ids or None,
        "rule_ids": rule_ids or None,
    }
    metadata = {k: v for k, v in metadata.items() if v is not None}

    if _store_security_audit is None:
        raise RuntimeError("security audit storage has not been configured")
    await _store_security_audit(
        message_id=f"audit-{request_id or int(time.time() * 1000)}-{decision}",
        chat_jid=chat_jid,
        sender="security",
        sender_name="security",
        content=json.dumps(metadata),
        timestamp=datetime.now(UTC).isoformat(),
        is_from_me=True,
        message_type="security_audit",
        metadata=metadata,
    )


async def prune_security_audit(retention_days: int = 30) -> int:
    """Delete security audit entries older than retention period.

    Only deletes rows with sender='security' — chat history is untouched.
    Returns the number of rows deleted.
    """
    cutoff_ts = time.time() - (retention_days * 86400)
    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=UTC).isoformat()
    if _prune_security_audit is None:
        raise RuntimeError("security audit storage has not been configured")
    return await _prune_security_audit("security", cutoff_iso)
