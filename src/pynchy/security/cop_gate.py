"""Cop gate for host-mutating IPC operations.

Integrates the Cop inspector with the approval state machine.
Host-mutating operations are always inspected; human approval is
triggered only if the Cop flags something suspicious.

Flow:
  1. IPC handler calls cop_gate() before executing a host-mutating op
  2. cop_gate() calls inspect_outbound() from the Cop module
  3. If Cop flags it: creates pending approval with handler_type="ipc"
  4. User approves/denies via the normal approval channel
  5. process_approval_decision() dispatches through ipc._registry.dispatch()
  6. Re-dispatched request has _cop_approved=True, so the handler skips the gate

See docs/plans/2026-02-24-host-mutating-cop-design.md
"""

from __future__ import annotations

from typing import Any

from pynchy.ipc._deps import IpcDeps, resolve_chat_jid
from pynchy.logger import logger
from pynchy.security.approval import create_pending_approval, format_approval_notification
from pynchy.security.audit import record_security_event
from pynchy.security.cop import inspect_outbound


async def cop_gate(
    operation: str,
    payload_summary: str,
    data: dict[str, Any],
    source_group: str,
    deps: IpcDeps,
    *,
    request_id: str | None = None,
) -> bool:
    """Gate a host-mutating operation through the Cop.

    Returns True if the operation should proceed, False if it was
    escalated to human approval (or blocked outright).

    Args:
        operation: The IPC operation name (e.g., "sync_worktree_to_main")
        payload_summary: Human-readable summary of what the operation will do
        data: The full IPC request data dict
        source_group: The group folder that originated the request
        deps: IPC dependency protocol for workspace lookup and broadcasting
        request_id: If set, a pending approval is created on flag (request-reply).
            If None, the operation is fire-and-forget and gets a broadcast warning only.
    """
    verdict = await inspect_outbound(operation, payload_summary)

    # Resolve chat_jid for audit and notifications
    chat_jid = resolve_chat_jid(source_group, deps) or "unknown"

    decision = "cop_flagged" if verdict.flagged else "cop_allowed"
    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=operation,
        decision=decision,
        reason=verdict.reason,
    )

    # Notify user of audit result (token stream transparency)
    if verdict.flagged:
        await deps.broadcast_host_message(
            chat_jid,
            f"\U0001f46e FAIL {operation}: {verdict.reason}",
        )
    else:
        await deps.broadcast_host_message(
            chat_jid, f"\U0001f46e pass {operation}"
        )

    if not verdict.flagged:
        return True

    logger.warning(
        "Cop flagged host-mutating operation",
        operation=operation,
        source_group=source_group,
        reason=verdict.reason,
    )

    if request_id:
        # Request-reply: create a pending approval so the user can approve/deny
        create_pending_approval(
            request_id=request_id,
            tool_name=operation,
            source_group=source_group,
            chat_jid=chat_jid,
            request_data=data,
            handler_type="ipc",
        )

        short_id = request_id[:8]
        notification = format_approval_notification(operation, data, short_id)
        notification = f"[Cop flagged: {verdict.reason}]\n\n{notification}"
        await deps.broadcast_to_channels(chat_jid, notification)
    else:
        # Fire-and-forget: no approval possible, just warn
        await deps.broadcast_to_channels(
            chat_jid,
            f"[Cop blocked] {operation} from {source_group}: {verdict.reason}\n"
            f"(fire-and-forget \u2014 no approval possible)",
        )

    return False
