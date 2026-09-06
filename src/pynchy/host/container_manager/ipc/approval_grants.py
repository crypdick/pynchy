"""Apply validated reusable human-approval choices."""

from __future__ import annotations

import asyncio
from typing import cast

from pynchy.host.container_manager.ipc.approval_replay import (
    ApprovalDecisionContext,
    ApprovalReplayPolicy,
    approval_replay_validation_error,
)
from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,
)
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security import approval as security_approval
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.gate import get_gate_for_group
from pynchy.logger import logger
from pynchy.plugins.api import ApprovalMode


async def apply_reusable_approval(
    context: ApprovalDecisionContext,
    deps: IpcDeps,
) -> bool:
    """Apply a session or permanent grant after exact-request replay validation."""
    if context.approval_scope == "once":
        return True
    has_semantic_capability = context.handler_type == "mcp_proxy" or (
        context.handler_type == "service" and context.action is not None
    )
    if not has_semantic_capability or context.capability_id is None or context.gate is None:
        return await _reject(context, deps, "Approval lacks a current semantic capability.")
    capability_id = context.capability_id
    if context.approval_scope == "session":
        context.gate.grant_session_capability_approval(capability_id)
        if context.action is not None and context.action.approval.mode is ApprovalMode.SESSION_TOOL:
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


async def resolve_mcp_proxy_approval_decision(
    context: ApprovalDecisionContext,
    deps: IpcDeps | None,
    replay_policy: ApprovalReplayPolicy,
) -> None:
    """Apply reusable scope, then resolve one waiting MCP proxy request."""
    approved = context.approved
    resolution_audited = False
    if approved and context.approval_scope != "once":
        validation_error = (
            "Approval replay dependencies are unavailable."
            if deps is None
            else await approval_replay_validation_error(context, deps, replay_policy)
        )
        if validation_error is not None:
            approved = False
            if deps is not None:
                await deps.broadcast_host_message(
                    context.chat_jid,
                    f"Approval is stale: {validation_error}",
                )
            await record_security_event(
                chat_jid=context.chat_jid,
                workspace=context.source_group,
                tool_name=context.tool_name,
                decision="execution_failed",
                reason=validation_error,
                request_id=context.request_id,
                capability_id=context.capability_id,
                action_ids=context.action_ids,
            )
            resolution_audited = True
        elif not await apply_reusable_approval(context, cast("IpcDeps", deps)):
            approved = False
            resolution_audited = True
    resolved = security_approval.resolve_mcp_proxy_approval(context.request_id, approved=approved)
    if not resolved:
        logger.warning(
            "MCP proxy approval Future not found (timed out?)",
            request_id=context.request_id,
        )
    if not resolution_audited:
        await record_security_event(
            chat_jid=context.chat_jid,
            workspace=context.source_group,
            tool_name=context.tool_name,
            decision="approved_by_user" if approved else "denied_by_user",
            request_id=context.request_id,
            capability_id=context.capability_id,
            action_ids=context.action_ids,
        )


async def _reject(
    context: ApprovalDecisionContext,
    deps: IpcDeps,
    reason: str,
) -> bool:
    if context.handler_type != "mcp_proxy":
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
