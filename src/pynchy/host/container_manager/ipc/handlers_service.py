"""IPC handler for service requests dispatched to service handler plugins.

Service requests arrive from container MCP tools with type="service:<tool_name>".
This handler applies the workspace's security policy, then dispatches to
plugin-provided handlers discovered via the ``pynchy_service_handler`` hook.
"""

from __future__ import annotations

import sys
from collections.abc import (
    Awaitable,  # noqa: TC003, RUF100 - beartype resolves plugin handler signatures at runtime.
    Callable,  # noqa: TC003, RUF100 - beartype resolves plugin handler signatures at runtime.
)
from dataclasses import dataclass
from typing import Any

from pynchy.config import get_settings
from pynchy.host.container_manager.ipc.deps import IpcDeps, resolve_chat_jid
from pynchy.host.container_manager.ipc.registry import register_prefix
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    get_gate_for_group,
    resolve_security,
)
from pynchy.logger import logger
from pynchy.plugins import get_plugin_manager

# Lazily populated mapping of tool_name -> async handler from plugins.
_plugin_handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] | None = None


@dataclass(frozen=True)
class _ServiceRequest:
    full_type: str
    tool_name: str
    request_id: str


def _get_plugin_handlers() -> dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]:
    """Collect and cache tool handlers from all MCP server plugins."""
    handlers = _plugin_handlers
    if handlers is not None:
        return handlers

    pm = get_plugin_manager()
    merged: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {}
    for result in pm.hook.pynchy_service_handler():
        tools = result.get("tools", {})
        merged.update(tools)

    sys.modules[__name__].__dict__["_plugin_handlers"] = merged
    return merged


def clear_plugin_handler_cache() -> None:
    """Clear the cached plugin handler mapping (for tests or config reload)."""
    sys.modules[__name__].__dict__["_plugin_handlers"] = None


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
    *,
    request: _ServiceRequest,
    data: dict[str, Any],
    source_group: str,
    chat_jid: str,
    deps: IpcDeps,
    gate: SecurityGate,
    reason: str | None,
) -> None:
    # Lazy import to avoid circular: security.approval → ipc._write → ipc.__init__ → here
    from pynchy.host.container_manager.security.approval import (
        create_pending_approval,
        format_approval_notification,
    )
    from pynchy.types import OutboundEvent, OutboundEventType

    short_id = create_pending_approval(
        request_id=request.request_id,
        tool_name=request.tool_name,
        source_group=source_group,
        chat_jid=chat_jid,
        request_data=data,
    )

    notification = format_approval_notification(request.tool_name, data, short_id)
    await deps.broadcast_to_channels(
        chat_jid, OutboundEvent(type=OutboundEventType.SYSTEM, content=notification)
    )

    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=request.tool_name,
        decision="approval_requested",
        corruption_tainted=gate.policy.corruption_tainted,
        secret_tainted=gate.policy.secret_tainted,
        reason=reason,
        request_id=request.request_id,
    )
    logger.info(
        "Service request needs human approval",
        tool_name=request.tool_name,
        source_group=source_group,
        short_id=short_id,
        reason=reason,
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

    from pynchy.host.container_manager.security.cop_gate import cop_gate

    settings = get_settings()
    from pynchy.config.models import McpTool

    tool = settings.tools.get(request.tool_name)
    if not isinstance(tool, McpTool) or tool.mcp.runtime != "script":
        return True

    import json as json_mod

    args_preview = json_mod.dumps(
        {k: v for k, v in data.items() if k not in ("type", "request_id", "source_group")},
        default=str,
    )[:1000]
    summary = f"script MCP tool: {request.tool_name}\nargs: {args_preview}"
    return await cop_gate(
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

    # Look up handler from plugins
    handlers = _get_plugin_handlers()
    handler = handlers.get(request.tool_name)

    if handler is None:
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

    # Evaluate policy — service requests are writes (they perform actions)
    decision = gate.evaluate_write(request.tool_name, data)

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
            request=request,
            data=data,
            source_group=source_group,
            chat_jid=chat_jid,
            deps=deps,
            gate=gate,
            reason=decision.reason,
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
    )

    logger.info(
        "Service request allowed by policy",
        tool_name=request.tool_name,
        source_group=source_group,
    )

    data["source_group"] = source_group
    response = await handler(data)
    _write_response(source_group, request.request_id, response)


# Register a prefix handler so all "service:*" IPC types route here.
# The handler itself resolves plugin-provided tool handlers lazily.
register_prefix("service:", _handle_service_request)
