"""Approval command handlers for the chat pipeline.

Handles ``approve <id>``, ``deny <id>``, and ``pending`` commands by
writing decision files that the IPC watcher picks up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from pynchy.logger import logger
from pynchy.security.approval import (
    _approval_decisions_dir,
    find_pending_by_short_id,
    list_pending_approvals,
)
from pynchy.utils import write_json_atomic


class ApprovalDeps(Protocol):
    """Minimal deps needed by approval handlers."""

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...


async def handle_approval_command(
    deps: ApprovalDeps,
    chat_jid: str,
    action: str,
    short_id: str,
    sender: str,
) -> None:
    """Process an approve/deny command by writing a decision file.

    The IPC watcher picks up the decision file and executes or
    denies the original request.
    """
    pending = find_pending_by_short_id(short_id)

    if pending is None:
        await deps.broadcast_host_message(
            chat_jid,
            f"No pending approval found for ID: {short_id}",
        )
        return

    request_id = pending["request_id"]
    source_group = pending["source_group"]
    approved = action == "approve"

    decisions_dir = _approval_decisions_dir(source_group)
    decision_data = {
        "request_id": request_id,
        "approved": approved,
        "decided_by": sender,
        "decided_at": datetime.now(UTC).isoformat(),
    }

    write_json_atomic(decisions_dir / f"{request_id}.json", decision_data, indent=2)

    verb = "Approved" if approved else "Denied"
    await deps.broadcast_host_message(
        chat_jid,
        f"\u2705 {verb}: {pending['tool_name']} ({short_id})",
    )

    logger.info(
        "Approval decision written",
        request_id=request_id,
        action=action,
        decided_by=sender,
    )


async def handle_pending_query(deps: ApprovalDeps, chat_jid: str) -> None:
    """List all pending approval requests."""
    pending = list_pending_approvals()

    if not pending:
        await deps.broadcast_host_message(chat_jid, "No pending approvals.")
        return

    lines = ["Pending approvals:\n"]
    for p in pending:
        lines.append(
            f"  \u2022 {p['tool_name']} ({p['short_id']}) \u2014 {p.get('source_group', '?')}"
        )

    await deps.broadcast_host_message(chat_jid, "\n".join(lines))
