"""IPC handler for service requests dispatched to service handler plugins.

Service requests arrive from container MCP tools with type="service:<tool_name>".
This handler applies the workspace's security policy, then dispatches to
plugin-provided handlers discovered via the ``pynchy_service_handler`` hook.
"""

from __future__ import annotations

import json as json_mod
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves service runtime annotations.
)
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pynchy.action_intents import ActionIntent  # noqa: TC001 - beartype resolves dataclass.
from pynchy.host.container_manager.ipc.deps import IpcDeps, resolve_chat_jid
from pynchy.host.container_manager.ipc.registry import register_prefix
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security import cop_gate as cop_gate_module
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    SecuritySettings,
    build_workspace_security,
    evaluate_host_action_policy,
    get_gate_for_group,
    resolve_security,
)
from pynchy.identifiers import ChatJid
from pynchy.logger import logger
from pynchy.plugins.api import (  # beartype resolves these runtime annotations.
    HostActionCatalog,
    HostActionDescriptor,
    clear_host_action_catalog_cache,
    get_host_action_catalog,
    missing_workspace_tool,
)
from pynchy.workspace.api import (
    ResolvedWorkspaceConfig,  # noqa: TC001 - beartype resolves workspace policy annotations.
)


@runtime_checkable
class _McpRuntime(Protocol):
    runtime: str


@runtime_checkable
class _McpTool(Protocol):
    type: str
    mcp: _McpRuntime


def _unconfigured_settings() -> SecuritySettings:
    raise RuntimeError("Service configuration has not been composed")


def _unconfigured_resolved_config(
    _source_group: str, _settings: SecuritySettings | None
) -> ResolvedWorkspaceConfig | None:
    raise RuntimeError("Service workspace resolution has not been composed")


def _unconfigured_matrix_route(_source_group: str) -> object | None:
    raise RuntimeError("Service route policy has not been composed")


_get_settings: Callable[[], SecuritySettings] = _unconfigured_settings
load_resolved_config: Callable[[str, SecuritySettings | None], ResolvedWorkspaceConfig | None] = (
    _unconfigured_resolved_config
)
get_active_matrix_route: Callable[[str], object | None] = _unconfigured_matrix_route


def configure_service_runtime(
    *,
    get_settings: Callable[[], SecuritySettings],
    resolve_workspace_config: Callable[
        [str, SecuritySettings | None], ResolvedWorkspaceConfig | None
    ],
    active_matrix_route: Callable[[str], object | None],
) -> None:
    """Bind configuration and route policy sources at host composition."""
    global _get_settings, load_resolved_config, get_active_matrix_route  # noqa: PLW0603 - one host process owns these policy sources.
    _get_settings = get_settings
    load_resolved_config = resolve_workspace_config
    get_active_matrix_route = active_matrix_route


def get_settings() -> SecuritySettings:
    return _get_settings()


@dataclass(frozen=True)
class _ServiceRequest:
    full_type: str
    tool_name: str
    request_id: str


@dataclass(frozen=True)
class _ApprovalRequestContext:
    request: _ServiceRequest
    action: HostActionDescriptor
    data: dict[str, Any]
    source_group: str
    chat_jid: str
    deps: IpcDeps
    gate: SecurityGate
    reason: str | None
    intent: ActionIntent | None


def _get_action_catalog() -> HostActionCatalog:
    """Return the single catalog shared by policy, dispatch, and status."""
    return get_host_action_catalog()


def clear_plugin_handler_cache() -> None:  # noqa: V103
    """Clear the cached plugin handler mapping (for tests or config reload)."""
    clear_host_action_catalog_cache()


def _write_response(source_group: str, request_id: str, response: dict[str, Any]) -> None:
    """Write a response file for the container to pick up."""
    write_ipc_response(ipc_response_path(source_group, request_id), response)


def _service_request(data: dict[str, Any], source_group: str) -> _ServiceRequest | None:
    full_type = data.get("type")
    request_id = data.get("request_id")
    if not isinstance(full_type, str) or not isinstance(request_id, str) or not request_id:
        logger.warning(
            "Service request missing request_id",
            type=full_type,
            source_group=source_group,
        )
        return None
    return _ServiceRequest(
        full_type=full_type,
        tool_name=full_type.removeprefix("service:"),
        request_id=request_id,
    )


def _security_gate(source_group: str, *, is_admin: bool) -> SecurityGate | None:
    """Resolve dispatch policy without dropping runtime route restrictions."""
    gate = get_gate_for_group(source_group)
    if gate is not None:
        return gate

    resolved = load_resolved_config(source_group, None)
    if resolved is None:
        if get_active_matrix_route(source_group) is not None:
            return None
        return SecurityGate(resolve_security(source_group, is_admin=is_admin))
    security = build_workspace_security(get_settings(), resolved)
    logger.warning(
        "No SecurityGate for group, created ephemeral",
        source_group=source_group,
    )
    return SecurityGate(security)


def _service_action_and_gate(
    request: _ServiceRequest,
    source_group: str,
    *,
    is_admin: bool,
) -> tuple[HostActionDescriptor, SecurityGate] | None:
    action = _get_action_catalog().action_for(request.tool_name)
    if action is None:
        logger.warning(
            "Unknown service tool type",
            tool_name=request.tool_name,
            source_group=source_group,
        )
        _write_response(
            source_group,
            request.request_id,
            {"error": f"Unknown service tool: {request.tool_name}"},
        )
        return None
    resolved = load_resolved_config(source_group, None)
    active_route = get_active_matrix_route(source_group)
    if active_route is not None and resolved is None:
        logger.warning(
            "Route-scoped service tool policy is unavailable",
            tool_name=request.tool_name,
            source_group=source_group,
        )
        _write_response(
            source_group,
            request.request_id,
            {"error": f"Service tool is not enabled for this route: {request.tool_name}"},
        )
        return None
    if resolved is not None:
        configured_security = build_workspace_security(get_settings(), resolved)
        missing_tool = missing_workspace_tool(
            action,
            resolved.tools,
            service_aliases=configured_security.services,
        )
    else:
        missing_tool = None
    if missing_tool is not None:
        # Host dispatch is authoritative even though the built-in MCP proxy
        # advertises a stable tool schema to every agent runtime.
        logger.warning(
            "Service tool capability is not enabled for workspace",
            tool_name=request.tool_name,
            required_tool=missing_tool,
            source_group=source_group,
        )
        _write_response(
            source_group,
            request.request_id,
            {"error": f"Service tool is not enabled for this route: {request.tool_name}"}
            if active_route is not None
            else {
                "error": (
                    "Host capability unavailable: "
                    f"Tool {missing_tool} is not enabled for this workspace"
                )
            },
        )
        return None
    gate = _security_gate(source_group, is_admin=is_admin)
    if gate is None:
        _write_response(
            source_group,
            request.request_id,
            {"error": "Workspace security policy is unavailable; refusing host action"},
        )
        return None
    return action, gate


async def _request_human_approval(
    context: _ApprovalRequestContext,
) -> None:
    # Lazy import to avoid circular: security.approval → ipc._write → ipc.__init__ → here
    from pynchy.host.container_manager.security.approval import (  # noqa: PLC0415 - security.approval imports IPC dispatch during approval replay.
        approval_event,
        create_pending_approval,
    )

    control = await context.deps.get_conversation_control_by_thread(ChatJid(context.chat_jid))
    short_id = create_pending_approval(
        request_id=context.request.request_id,
        tool_name=context.request.tool_name,
        source_group=context.source_group,
        approval_chat_jid=context.chat_jid,
        request_data=context.data,
        expires_after_seconds=context.action.approval.expires_after_seconds,
        approval_scope=context.action.approval.mode.value,
        capability_id=str(context.action.capability.id),
        origin_conversation_id=(str(control.conversation_id) if control is not None else None),
        action_payload=(context.intent.payload if context.intent is not None else None),
        corruption_tainted=context.gate.corruption_tainted,
        secret_tainted=context.gate.secret_tainted,
    )
    if context.action.action_intent is not None:
        await context.deps.mark_action_intent_awaiting_approval(
            context.request.request_id,
            policy_decision=context.reason or "human approval required",
        )

    await context.deps.broadcast_to_channels(
        context.chat_jid,
        approval_event(
            context.request.tool_name,
            context.data,
            short_id,
            capability_id=str(context.action.capability.id),
        ),
    )

    await record_security_event(
        chat_jid=context.chat_jid,
        workspace=context.source_group,
        tool_name=context.request.tool_name,
        decision="approval_requested",
        corruption_tainted=context.gate.corruption_tainted,
        secret_tainted=context.gate.secret_tainted,
        reason=context.reason,
        request_id=context.request.request_id,
        capability_id=str(context.action.capability.id),
        action_ids=tuple(str(action_id) for action_id in context.action.capability.action_ids),
    )
    logger.info(
        "Service request needs human approval",
        tool_name=context.request.tool_name,
        source_group=context.source_group,
        short_id=short_id,
        reason=context.reason,
    )


async def _maybe_require_cop_approval(
    *,
    request: _ServiceRequest,
    data: dict[str, Any],
    source_group: str,
    deps: IpcDeps,
) -> bool:
    settings = get_settings()

    tool = settings.tools.get(request.tool_name)
    if (
        not isinstance(tool, _McpTool)
        or tool.type != "mcp"
        or tool.mcp.runtime not in ("script", "stdio")
    ):
        return True

    operation = f"script_mcp:{request.tool_name}"
    receipt = await cop_gate_module.verify_approval_receipt(operation, data, source_group, deps)
    if receipt is cop_gate_module.ReceiptVerification.INVALID:
        _write_response(
            source_group,
            request.request_id,
            {"error": "Invalid or replayed approval receipt"},
        )
        return False
    if receipt is cop_gate_module.ReceiptVerification.VALID:
        return True

    args_preview = json_mod.dumps(
        {k: v for k, v in data.items() if k not in ("type", "request_id", "source_group")},
        default=str,
    )[:1000]
    summary = f"host MCP tool: {request.tool_name}\nargs: {args_preview}"
    return await cop_gate_module.cop_gate(
        operation,
        summary,
        data,
        source_group,
        deps,
        request_id=request.request_id,
    )


async def _handle_service_request(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered prefix handler keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    """Handle a service request with policy enforcement and plugin dispatch."""
    request = _service_request(data, source_group)
    if request is None:
        return
    # The IPC envelope, not agent-controlled payload data, owns workspace
    # identity for route resolution, draft creation, and policy evaluation.
    data["source_group"] = source_group

    action_and_gate = _service_action_and_gate(
        request,
        source_group,
        is_admin=is_admin,
    )
    if action_and_gate is None:
        return
    action, gate = action_and_gate

    # Find the chat_jid for this group (for audit logging)
    chat_jid = resolve_chat_jid(source_group, deps) or "unknown"

    intent, replay_response = await deps.prepare_action_intent(
        action,
        data,
        workspace=source_group,
        chat_jid=chat_jid,
        request_id=request.request_id,
    )
    if replay_response is not None:
        _write_response(source_group, request.request_id, replay_response)
        return

    # Host-side integrations declare their non-mutating operations explicitly.
    # Reading an untrusted source must taint the turn before an agent can use
    # that content to influence a later external action.
    decision = evaluate_host_action_policy(action, gate, data)

    if not decision.allowed:
        if intent is not None:
            await deps.deny_action_intent(
                request.request_id,
                reason=f"Policy denied: {decision.reason}",
            )
        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=request.tool_name,
            decision="blocked_forbidden",
            corruption_tainted=gate.corruption_tainted,
            secret_tainted=gate.secret_tainted,
            reason=decision.reason,
            request_id=request.request_id,
            capability_id=str(action.capability.id),
            action_ids=tuple(str(action_id) for action_id in action.capability.action_ids),
        )
        _write_response(
            source_group,
            request.request_id,
            {"error": f"Policy denied: {decision.reason}"},
        )
        logger.info(
            "Service request denied by policy",
            tool_name=request.tool_name,
            source_group=source_group,
            reason=decision.reason,
        )
        return

    if decision.needs_human:
        await _request_human_approval(
            _ApprovalRequestContext(
                request=request,
                action=action,
                data=data,
                source_group=source_group,
                chat_jid=chat_jid,
                deps=deps,
                gate=gate,
                reason=decision.reason,
                intent=intent,
            )
        )
        # No response file written — container blocks until human decides
        return

    if not await _maybe_require_cop_approval(
        request=request,
        data=data,
        source_group=source_group,
        deps=deps,
    ):
        return

    if intent is not None:
        await deps.approve_action_intent(
            request.request_id,
            approver="policy",
            approved_at=deps.policy_approval_timestamp(),
            policy_decision=decision.reason or "allowed by current policy",
        )

    # Allowed — record audit and dispatch to plugin handler
    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=request.tool_name,
        decision="allowed",
        corruption_tainted=gate.corruption_tainted,
        secret_tainted=gate.secret_tainted,
        reason=decision.reason,
        request_id=request.request_id,
        capability_id=str(action.capability.id),
        action_ids=tuple(str(action_id) for action_id in action.capability.action_ids),
    )

    logger.info(
        "Service request allowed by policy",
        tool_name=request.tool_name,
        source_group=source_group,
    )

    try:
        response = await deps.execute_action_intent(action, data, request_id=request.request_id)
    except (
        Exception
    ) as exc:  # host action boundary must audit terminal failure before watcher recovery.
        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=request.tool_name,
            decision="execution_failed",
            corruption_tainted=gate.corruption_tainted,
            secret_tainted=gate.secret_tainted,
            reason=type(exc).__name__,
            request_id=request.request_id,
            capability_id=str(action.capability.id),
            action_ids=tuple(str(action_id) for action_id in action.capability.action_ids),
        )
        raise
    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=request.tool_name,
        decision="execution_failed" if "error" in response else "execution_succeeded",
        corruption_tainted=gate.corruption_tainted,
        secret_tainted=gate.secret_tainted,
        reason="handler returned an error" if "error" in response else None,
        request_id=request.request_id,
        capability_id=str(action.capability.id),
        action_ids=tuple(str(action_id) for action_id in action.capability.action_ids),
    )
    _write_response(source_group, request.request_id, response)


# Register a prefix handler so all "service:*" IPC types route here.
# The handler itself resolves plugin-provided tool handlers lazily.
register_prefix("service:", _handle_service_request)
