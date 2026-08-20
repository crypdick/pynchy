"""Apply validated reusable human-approval choices."""

from __future__ import annotations

import asyncio

from pynchy.host.container_manager.ipc.approval_replay import (
    ApprovalDecisionContext,  # noqa: TC001 - beartype resolves approval annotations.
)
from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # noqa: TC001 - beartype resolves approval annotations.
)
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.gate import get_gate_for_group
from pynchy.plugins.api import ApprovalMode


async def apply_reusable_approval(
    context: ApprovalDecisionContext,
    deps: IpcDeps,
) -> bool:
    """Apply a session or permanent grant after exact-request replay validation."""
    if context.approval_scope == "once":
        return True
    if context.handler_type != "service" or context.action is None or context.gate is None:
        return await _reject(context, deps, "Approval lacks a current semantic capability.")
    capability_id = str(context.action.capability.id)
    if context.approval_scope == "session":
        context.gate.grant_session_capability_approval(capability_id)
        if context.action.approval.mode is ApprovalMode.SESSION_TOOL:
            context.gate.grant_session_tool_approval(str(context.action.tool_name))
        active_gate = get_gate_for_group(context.source_group)
        if active_gate is not None and active_gate is not context.gate:
            active_gate.grant_session_capability_approval(capability_id)
        return True
    try:
        await asyncio.to_thread(
            deps.persist_capability_approval,
            context.source_group,
            capability_id,
        )
    except (OSError, TypeError, ValueError) as exc:
        return await _reject(context, deps, f"Could not approve forever: {exc}")
    return True


async def _reject(
    context: ApprovalDecisionContext,
    deps: IpcDeps,
    reason: str,
) -> bool:
    await deps.fail_action_intent(context.request_id, reason=reason)
    await asyncio.to_thread(
        write_ipc_response,
        ipc_response_path(context.source_group, context.request_id),
        {"error": reason},
    )
    await deps.broadcast_host_message(context.chat_jid, reason)
    await record_security_event(
        chat_jid=context.chat_jid,
        workspace=context.source_group,
        tool_name=context.tool_name,
        decision="execution_failed",
        reason=reason,
        request_id=context.request_id,
        capability_id=context.capability_id,
        action_ids=context.action_ids,
    )
    return False
