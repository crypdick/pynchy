"""Fail-closed evidence and policy checks for approved service replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import (
    Callable,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pynchy.conversation.api import (
    ConversationId,
)
from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,
)
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    get_gate_for_group,
    resolve_security,
)
from pynchy.plugins.api import (  # beartype resolves evidence annotations.
    HostActionDescriptor,
    missing_workspace_tool,
)
from pynchy.workspace.api import (
    WorkspaceSecurity,
)

type WorkspaceTools = tuple[str, ...] | None


@dataclass(frozen=True)
class ApprovalDecisionContext:
    """Durable evidence and current policy needed to resolve one decision."""

    request_id: str
    source_group: str
    tool_name: str
    chat_jid: str
    request_data: dict[str, Any]
    approved: bool
    approver: str
    approved_at: str
    handler_type: str
    action: HostActionDescriptor | None
    gate: SecurityGate | None
    capability_id: str | None
    action_ids: tuple[str, ...]
    origin_conversation_id: ConversationId | None
    action_payload: dict[str, Any] | None
    action_payload_sha256: str | None
    requested_at: str | None
    expires_after_seconds: int
    approval_scope: str = "once"


@dataclass(frozen=True)
class ApprovalReplayPolicy:
    """Current workspace policy required to replay an approved service action."""

    configured_security: Callable[[str], WorkspaceSecurity | None]
    workspace_tools: Callable[[str], WorkspaceTools]


async def approval_replay_validation_error(
    context: ApprovalDecisionContext,
    deps: IpcDeps,
    policy: ApprovalReplayPolicy,
) -> str | None:
    """Recheck exact payload, expiry, policy, and conversation presentation."""
    policy_error = _approval_policy_error(context, policy)
    if policy_error is not None:
        return policy_error
    time_error = _approval_time_error(context)
    if time_error is not None:
        return time_error
    payload_error = await _approval_payload_error(context, deps)
    if payload_error is not None:
        return payload_error
    return await _approval_conversation_error(context, deps)


def _approval_policy_error(
    context: ApprovalDecisionContext,
    policy: ApprovalReplayPolicy,
) -> str | None:
    if context.handler_type == "service" and context.gate is None:
        return "effective routed workspace policy is unavailable"
    if context.handler_type == "mcp_proxy":
        if context.gate is None or context.capability_id is None:
            return "effective MCP capability policy is unavailable"
        capability = context.gate.evaluate_capability(context.capability_id)
        if not capability.allowed:
            return capability.reason
    if context.handler_type == "service":
        selected_tools = policy.workspace_tools(context.source_group)
        if context.origin_conversation_id is not None and selected_tools is None:
            return "host tool is no longer enabled for this routed workspace"
        if (
            selected_tools is not None
            and context.action is not None
            and (
                missing_tool := missing_workspace_tool(
                    context.action,
                    list(selected_tools),
                    service_aliases=(
                        context.gate.service_names if context.gate is not None else ()
                    ),
                )
            )
            is not None
        ):
            return f"host tool {missing_tool} is no longer enabled for this workspace"
    return None


def _approval_time_error(context: ApprovalDecisionContext) -> str | None:
    if context.requested_at is None:
        return "approval request has no timestamp"
    try:
        requested_at = datetime.fromisoformat(context.requested_at)
    except ValueError:
        return "approval request timestamp is invalid"
    if requested_at.tzinfo is None:
        return "approval request timestamp has no timezone"
    if (datetime.now(UTC) - requested_at).total_seconds() > context.expires_after_seconds:
        return "approval request expired"
    return None


async def _approval_payload_error(context: ApprovalDecisionContext, deps: IpcDeps) -> str | None:
    if context.action_payload is not None:
        payload_hash = hashlib.sha256(
            json.dumps(context.action_payload, sort_keys=True).encode()
        ).hexdigest()
        if payload_hash != context.action_payload_sha256:
            return "approval payload evidence is corrupt"
        intent = await deps.get_action_intent_by_request(context.request_id)
        if intent is None or intent.payload != context.action_payload:
            return "approved action payload no longer matches its durable intent"
        binding_error = _bound_presentation_error(context)
        if binding_error is not None:
            return binding_error
    return None


def _bound_presentation_error(context: ApprovalDecisionContext) -> str | None:
    """Cross-check route-bound presentation against the pending approval."""
    payload = context.action_payload
    if payload is None or not {
        "conversation_id",
        "approval_chat_jid",
    }.intersection(payload):
        return None
    if context.origin_conversation_id is None:
        return "route-bound approval lost its originating conversation"
    if payload.get("conversation_id") != context.origin_conversation_id:
        return "approval conversation does not match its bound action payload"
    if payload.get("approval_chat_jid") != context.chat_jid:
        return "approval destination does not match its bound action payload"
    return None


async def _approval_conversation_error(
    context: ApprovalDecisionContext, deps: IpcDeps
) -> str | None:
    if context.origin_conversation_id is not None:
        binding = await deps.get_conversation_control_binding(context.origin_conversation_id)
        if binding is None or binding.thread_jid != context.chat_jid:
            return "conversation control binding changed; request a new approval"
    return None


def approval_replay_gate(
    source_group: str,
    *,
    policy: ApprovalReplayPolicy,
    require_resolved: bool = False,
    request_corruption_tainted: bool = False,
    request_secret_tainted: bool = False,
) -> SecurityGate | None:
    """Rebuild current policy while retaining sticky taint from the active gate."""
    active_gate = get_gate_for_group(source_group)
    if policy.workspace_tools(source_group) is None:
        if require_resolved:
            return None
        gate = active_gate or SecurityGate(resolve_security(source_group))
    else:
        security = policy.configured_security(source_group)
        if security is None:
            return None
        gate = SecurityGate(security)
    if request_corruption_tainted or (active_gate is not None and active_gate.corruption_tainted):
        gate.notify_public_source_input()
    if request_secret_tainted or (active_gate is not None and active_gate.secret_tainted):
        gate.notify_secret_source_input()
    return gate
