"""Security audit logging via the existing messages table.

Stores policy evaluation results as messages with sender='security'
and message_type='security_audit'. Uses the existing messages table
so no schema changes are needed.

Retention pruning is scoped to security rows only — chat history
is untouched.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from pynchy.db._connection import _get_db
from pynchy.db.messages import store_message_direct


async def record_security_event(
    chat_jid: str,
    workspace: str,
    tool_name: str,
    decision: str,  # "allowed", "denied", "approval_requested", "rate_limited"
    *,
    tier: str | None = None,
    reason: str | None = None,
    request_id: str | None = None,
) -> None:
    """Record a policy evaluation in the messages table."""
    metadata = {
        "workspace": workspace,
        "tool_name": tool_name,
        "decision": decision,
        "tier": tier,
        "reason": reason,
        "request_id": request_id,
    }
    metadata = {k: v for k, v in metadata.items() if v is not None}

    await store_message_direct(
        id=f"audit-{request_id or int(time.time() * 1000)}",
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
    db = _get_db()
    cursor = await db.execute(
        "DELETE FROM messages WHERE sender = 'security' AND timestamp < ?",
        (cutoff_iso,),
    )
    await db.commit()
    return cursor.rowcount
