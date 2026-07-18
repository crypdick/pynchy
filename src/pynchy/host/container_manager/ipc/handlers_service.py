"""IPC handler for service requests dispatched to service handler plugins.

Service requests arrive from container MCP tools with type="service:<tool_name>".
This handler applies the workspace's security policy, then dispatches to
plugin-provided handlers discovered via the ``pynchy_service_handler`` hook.
"""

from __future__ import annotations

import json as json_mod
from dataclasses import dataclass
from typing import Any

from pynchy.capabilities import (  # noqa: TC001, RUF100 - beartype resolves these runtime annotations.
    HostActionDescriptor,
    HostActionHandler,
)
from pynchy.config import get_settings
from pynchy.config.models import McpTool
from pynchy.host.container_manager.ipc.deps import IpcDeps, resolve_chat_jid
from pynchy.host.container_manager.ipc.registry import register_prefix
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security import cop_gate as cop_gate_module
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    evaluate_host_action_policy,
    get_gate_for_group,
    resolve_security,
)
from pynchy.logger import logger
from pynchy.plugins.host_actions import (
    HostActionCatalog,
    clear_host_action_catalog_cache,
    get_host_action_catalog,
)


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


def _get_plugin_handlers() -> dict[str, HostActionHandler]:
    """Return handlers keyed by tool name from the typed catalog."""
    return get_host_action_catalog().handlers


def _get_action_catalog() -> HostActionCatalog:
    """Return the single catalog shared by policy, dispatch, and status."""
    return get_host_action_catalog()


def clear_plugin_handler_cache() -> None:
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


def _security_gate(source_group: str, *, is_admin: bool) -> SecurityGate:
    gate = get_gate_for_group(source_group)
    if gate is not None:
        return gate

    security = resolve_security(source_group, is_admin=is_admin)
    logger.warning(
        "No SecurityGate for group, created ephemeral",
        source_group=source_group,
    )
    return SecurityGate(security)


async def _request_human_approval(
    context: _ApprovalRequestContext,
) -> None:
    # Lazy import to avoid circular: security.approval → ipc._write → ipc.__init__ → here
    from pynchy.host.container_manager.security.approval import (  # noqa: PLC0415, RUF100 - security.approval imports IPC dispatch during approval replay.
        approval_event,
        create_pending_approval,
    )

    short_id = create_pending_approval(
        request_id=context.request.request_id,
        tool_name=context.request.tool_name,
        source_group=context.source_group,
        chat_jid=context.chat_jid,
        request_data=context.data,
        expires_after_seconds=context.action.approval.expires_after_seconds,
    )

    await context.deps.broadcast_to_channels(
        context.chat_jid,
        approval_event(context.request.tool_name, context.data, short_id),
    )

    await record_security_event(
        chat_jid=context.chat_jid,
        workspace=context.source_group,
        tool_name=context.request.tool_name,
        decision="approval_requested",
        corruption_tainted=context.gate.policy.corruption_tainted,
        secret_tainted=context.gate.policy.secret_tainted,
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
    if data.get("_cop_approved"):
        return True

    settings = get_settings()

    tool = settings.tools.get(request.tool_name)
    if not isinstance(tool, McpTool) or tool.mcp.runtime != "script":
        return True

    args_preview = json_mod.dumps(
        {k: v for k, v in data.items() if k not in ("type", "request_id", "source_group")},
        default=str,
    )[:1000]
    summary = f"script MCP tool: {request.tool_name}\nargs: {args_preview}"
    return await cop_gate_module.cop_gate(
        f"script_mcp:{request.tool_name}",
        summary,
        data,
        source_group,
        deps,
        request_id=request.request_id,
    )


async def _handle_service_request(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001, RUF100 - registered prefix handler keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    """Handle a service request with policy enforcement and plugin dispatch."""
    request = _service_request(data, source_group)
    if request is None:
        return

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
        return

    gate = _security_gate(source_group, is_admin=is_admin)

    # Find the chat_jid for this group (for audit logging)
    chat_jid = resolve_chat_jid(source_group, deps) or "unknown"

    # Host-side integrations declare their non-mutating operations explicitly.
    # Reading an untrusted source must taint the turn before an agent can use
    # that content to influence a later external action.
    decision = evaluate_host_action_policy(action, gate, data)

    if not decision.allowed:
        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=request.tool_name,
            decision="blocked_forbidden",
            corruption_tainted=gate.policy.corruption_tainted,
            secret_tainted=gate.policy.secret_tainted,
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

    # Allowed — record audit and dispatch to plugin handler
    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=request.tool_name,
        decision="allowed",
        corruption_tainted=gate.policy.corruption_tainted,
        secret_tainted=gate.policy.secret_tainted,
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

    data["source_group"] = source_group
    try:
        response = await action.handler(data)
    except Exception as exc:  # noqa: BLE001, RUF100 - host action boundary must audit terminal failure before watcher recovery.
        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=request.tool_name,
            decision="execution_failed",
            corruption_tainted=gate.policy.corruption_tainted,
            secret_tainted=gate.policy.secret_tainted,
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
        corruption_tainted=gate.policy.corruption_tainted,
        secret_tainted=gate.policy.secret_tainted,
        reason="handler returned an error" if "error" in response else None,
        request_id=request.request_id,
        capability_id=str(action.capability.id),
        action_ids=tuple(str(action_id) for action_id in action.capability.action_ids),
    )
    _write_response(source_group, request.request_id, response)


# Register a prefix handler so all "service:*" IPC types route here.
# The handler itself resolves plugin-provided tool handlers lazily.
register_prefix("service:", _handle_service_request)
