"""Approval command handlers for the chat pipeline.

Handles ``approve <id>``, ``deny <id>``, and ``pending`` commands using
host-owned approval state that is never mounted into an agent runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pynchy.host.container_manager.security.approval import (
    _approval_decisions_dir,
    find_pending_by_short_id,
    list_pending_approvals,
)
from pynchy.logger import logger
from pynchy.utils import write_json_atomic


@runtime_checkable
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
    """Persist and immediately process an approve/deny command."""
    pending = find_pending_by_short_id(short_id)

    if pending is None or pending.get("approval_chat_jid") != chat_jid:
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
        "guarded_action_id": pending["guarded_action_id"],
        "request_payload_hash": pending["request_payload_hash"],
        "source_group": source_group,
        "approved": approved,
        "decided_by": sender,
        "decided_at": datetime.now(UTC).isoformat(),
    }

    decision_file = decisions_dir / f"{request_id}.json"
    write_json_atomic(decision_file, decision_data, indent=2)

    from pynchy.host.container_manager.ipc.handlers_approval import (  # noqa: PLC0415, RUF100 - lazy import avoids loading service dispatch until a human decides.
        process_approval_decision,
    )

    await process_approval_decision(decision_file, source_group, deps=deps)

    verb = "Approved" if approved else "Denied"
    await deps.broadcast_host_message(
        chat_jid,
        f"\u2705 {verb}: {pending['tool_name']} ({short_id})",
    )

    logger.info(
        "Approval decision processed",
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
    lines.extend(
        f"  \u2022 {p['tool_name']} ({p['short_id']}) \u2014 {p.get('source_group', '?')}"
        for p in pending
    )

    await deps.broadcast_host_message(chat_jid, "\n".join(lines))
