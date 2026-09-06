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
  6. Re-dispatched request carries a single-use, payload-bound receipt

See docs/plans/2026-02-24-host-mutating-cop-design.md
"""

from __future__ import annotations

from typing import Any

from pynchy.host.container_manager.ipc.deps import IpcDeps, resolve_chat_jid
from pynchy.host.container_manager.security.approval import (
    approval_event,
    create_pending_approval,
)
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.cop import (
    CopContextAvailability,
    CopInspectionContext,
    CopVerdict,
    inspect_outbound,
)
from pynchy.host.container_manager.security.gate import get_gate_for_group
from pynchy.host.container_manager.security.identity import (
    ReceiptVerification,
    consume_approval_receipt,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)


def cop_requires_human(verdict: CopVerdict, context: CopInspectionContext) -> bool:
    """Return whether a Cop result or bounded-context failure requires approval."""
    return (
        verdict.flagged
        or verdict.degraded
        or context.availability is CopContextAvailability.UNAVAILABLE
    )


async def verify_approval_receipt(
    operation: str,
    data: dict[str, Any],
    source_group: str,
    deps: IpcDeps,
) -> ReceiptVerification:
    """Consume a replay receipt and audit any forged or mismatched proof."""
    verification = consume_approval_receipt(
        data,
        workspace=source_group,
        operation=operation,
    )
    if verification is not ReceiptVerification.INVALID:
        return verification

    chat_jid = resolve_chat_jid(source_group, deps) or "unknown"
    request_id = data.get("request_id")
    safe_request_id = request_id if isinstance(request_id, str) else None
    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=operation,
        decision="approval_receipt_rejected",
        reason="Approval receipt was invalid, expired, replayed, or payload-mismatched",
        request_id=safe_request_id,
    )
    await deps.broadcast_host_message(
        chat_jid,
        f"Blocked {operation}: invalid or replayed approval receipt",
    )
    return verification


async def cop_gate(  # noqa: PLR0913 - gate boundary keeps the operation, payload, and dependency context explicit.
    operation: str,
    payload_summary: str,
    data: dict[str, Any],
    source_group: str,
    deps: IpcDeps,
    *,
    request_id: str | None = None,
    required_human_reason: str | None = None,
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
        required_human_reason: Skip model inspection and require approval because
            the caller could not supply trustworthy inspection evidence.
    """
    # Resolve chat_jid for audit and notifications
    chat_jid = resolve_chat_jid(source_group, deps) or "unknown"
    gate = get_gate_for_group(source_group)
    if gate is not None and not gate.cop_active:
        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=operation,
            decision="cop_disabled_by_profile",
            request_id=request_id,
        )
        return True

    inspection_context = await deps.load_cop_inspection_context(chat_jid)
    verdict = (
        CopVerdict(
            flagged=False,
            reason=required_human_reason,
            degraded=True,
        )
        if required_human_reason is not None
        else await inspect_outbound(operation, payload_summary, inspection_context)
    )
    context_degraded = inspection_context.availability is CopContextAvailability.UNAVAILABLE
    degraded = verdict.degraded or context_degraded

    correlation_id = request_id
    if correlation_id is None:
        raw_request_id = data.get("request_id")
        correlation_id = raw_request_id if isinstance(raw_request_id, str) else None
    decision = "cop_degraded" if degraded else "cop_flagged" if verdict.flagged else "cop_allowed"
    reason = verdict.reason
    if context_degraded:
        reason = "Bounded Cop context unavailable"
    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=operation,
        decision=decision,
        reason=reason,
        request_id=correlation_id,
    )

    # Notify user of audit result (token stream transparency)
    if verdict.flagged:
        await deps.broadcast_host_message(
            chat_jid,
            f"\U0001f46e FAIL {operation}: {verdict.reason}",
        )
    elif degraded:
        await deps.broadcast_host_message(
            chat_jid,
            f"\U0001f46e DEGRADED {operation}: human approval required",
        )
    else:
        await deps.broadcast_host_message(chat_jid, f"\U0001f46e pass {operation}")

    if not cop_requires_human(verdict, inspection_context):
        return True

    logger.warning(
        "Cop flagged host-mutating operation",
        operation=operation,
        source_group=source_group,
        reason=reason,
    )

    if request_id:
        # Request-reply: create a pending approval so the user can approve/deny
        short_id = create_pending_approval(
            request_id=request_id,
            tool_name=operation,
            source_group=source_group,
            approval_chat_jid=chat_jid,
            request_data=data,
            handler_type="ipc",
        )
        await deps.broadcast_to_channels(
            chat_jid,
            approval_event(
                operation,
                data,
                short_id,
                preface=(
                    f"[Cop unavailable or missing context: {reason}]"
                    if degraded
                    else f"[Cop flagged: {verdict.reason}]"
                ),
            ),
        )
    else:
        # Fire-and-forget: no approval possible, just warn
        msg = (
            f"[Cop blocked] {operation} from {source_group}: {reason}\n"
            f"(fire-and-forget \u2014 no approval possible)"
        )
        await deps.broadcast_to_channels(
            chat_jid, OutboundEvent(type=OutboundEventType.SYSTEM, content=msg)
        )

    return False
