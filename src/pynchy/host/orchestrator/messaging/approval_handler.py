"""Approval command handlers for the chat pipeline.

Handles ``approve <id>``, ``deny <id>``, and ``pending`` commands using
host-owned approval state that is never mounted into an agent runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pynchy.host.orchestrator.messaging.deps import (  # noqa: TC001 - beartype resolves protocol annotations.
    ApprovalRuntimeOperations,
)
from pynchy.logger import logger


@runtime_checkable
class ApprovalDeps(Protocol):
    """Minimal deps needed by approval handlers."""

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    @property
    def approval_runtime_operations(self) -> ApprovalRuntimeOperations: ...


async def handle_approval_command(
    deps: ApprovalDeps,
    chat_jid: str,
    action: str,
    short_id: str,
    sender: str,
) -> None:
    """Persist and immediately process an approve/deny command."""
    operations = deps.approval_runtime_operations
    pending = operations.find_pending_by_short_id(short_id)

    if pending is None or pending.get("approval_chat_jid") != chat_jid:
        await deps.broadcast_host_message(
            chat_jid,
            f"No pending approval found for ID: {short_id}",
        )
        return

    request_id = pending["request_id"]
    source_group = pending["source_group"]
    approved = action == "approve"

    decision_data = {
        "request_id": request_id,
        "guarded_action_id": pending["guarded_action_id"],
        "request_payload_hash": pending["request_payload_hash"],
        "source_group": source_group,
        "approved": approved,
        "decided_by": sender,
        "decided_at": datetime.now(UTC).isoformat(),
    }

    await operations.persist_and_process(source_group, decision_data)

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
    pending = deps.approval_runtime_operations.list_pending_approvals()

    if not pending:
        await deps.broadcast_host_message(chat_jid, "No pending approvals.")
        return

    lines = ["Pending approvals:\n"]
    lines.extend(
        f"  \u2022 {p['tool_name']} ({p['short_id']}) \u2014 {p.get('source_group', '?')}"
        for p in pending
    )

    await deps.broadcast_host_message(chat_jid, "\n".join(lines))
