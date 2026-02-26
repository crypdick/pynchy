"""IPC handler for service requests dispatched to service handler plugins.

Service requests arrive from container MCP tools with type="service:<tool_name>".
This handler applies the workspace's security policy, then dispatches to
plugin-provided handlers discovered via the ``pynchy_service_handler`` hook.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pynchy.config import get_settings
from pynchy.ipc._deps import IpcDeps, resolve_chat_jid
from pynchy.ipc._registry import register_prefix
from pynchy.ipc._write import ipc_response_path, write_ipc_response
from pynchy.logger import logger
from pynchy.plugin import get_plugin_manager
from pynchy.security.audit import record_security_event
from pynchy.security.gate import SecurityGate, get_gate_for_group, resolve_security

# Lazily populated mapping of tool_name -> async handler from plugins.
_plugin_handlers: dict[str, Callable[[dict], Awaitable[dict]]] | None = None


def _get_plugin_handlers() -> dict[str, Callable[[dict], Awaitable[dict]]]:
    """Collect and cache tool handlers from all MCP server plugins."""
    global _plugin_handlers  # noqa: PLW0603
    if _plugin_handlers is not None:
        return _plugin_handlers

    pm = get_plugin_manager()
    merged: dict[str, Callable[[dict], Awaitable[dict]]] = {}
    for result in pm.hook.pynchy_service_handler():
        tools = result.get("tools", {})
        merged.update(tools)

    _plugin_handlers = merged
    return _plugin_handlers


def clear_plugin_handler_cache() -> None:
    """Clear the cached plugin handler mapping (for tests or config reload)."""
    global _plugin_handlers  # noqa: PLW0603
    _plugin_handlers = None


def _write_response(source_group: str, request_id: str, response: dict) -> None:
    """Write a response file for the container to pick up."""
    write_ipc_response(ipc_response_path(source_group, request_id), response)


async def _handle_service_request(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    """Handle a service request with policy enforcement and plugin dispatch."""
    full_type = data.get("type", "")
    tool_name = full_type.removeprefix("service:")
    request_id = data.get("request_id")

    if not request_id:
        logger.warning(
            "Service request missing request_id",
            type=full_type,
            source_group=source_group,
        )
        return

    # Look up handler from plugins
    handlers = _get_plugin_handlers()
    handler = handlers.get(tool_name)

    if handler is None:
        logger.warning(
            "Unknown service tool type",
            tool_name=tool_name,
            source_group=source_group,
        )
        _write_response(
            source_group,
            request_id,
            {"error": f"Unknown service tool: {tool_name}"},
        )
        return

    # Look up session-scoped SecurityGate (created at container start).
    # Falls back to an ephemeral gate if none registered (e.g. during tests
    # or if the orchestrator hasn't created one yet).
    gate = get_gate_for_group(source_group)
    if gate is None:
        security = resolve_security(source_group, is_admin=is_admin)
        gate = SecurityGate(security)
        logger.warning(
            "No SecurityGate for group, created ephemeral",
            source_group=source_group,
        )

    # Find the chat_jid for this group (for audit logging)
    chat_jid = resolve_chat_jid(source_group, deps) or "unknown"

    # Evaluate policy — service requests are writes (they perform actions)
    decision = gate.evaluate_write(tool_name, data)

    if not decision.allowed:
        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=tool_name,
            decision="blocked_forbidden",
            corruption_tainted=gate.policy.corruption_tainted,
            secret_tainted=gate.policy.secret_tainted,
            reason=decision.reason,
            request_id=request_id,
        )
        _write_response(
            source_group,
            request_id,
            {"error": f"Policy denied: {decision.reason}"},
        )
        logger.info(
            "Service request denied by policy",
            tool_name=tool_name,
            source_group=source_group,
            reason=decision.reason,
        )
        return

    if decision.needs_human:
        # Lazy import to avoid circular: security.approval → ipc._write → ipc.__init__ → here
        from pynchy.security.approval import create_pending_approval, format_approval_notification

        short_id = request_id[:8]
        create_pending_approval(
            request_id=request_id,
            tool_name=tool_name,
            source_group=source_group,
            chat_jid=chat_jid,
            request_data=data,
        )

        notification = format_approval_notification(tool_name, data, short_id)
        await deps.broadcast_to_channels(chat_jid, notification)

        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=tool_name,
            decision="approval_requested",
            corruption_tainted=gate.policy.corruption_tainted,
            secret_tainted=gate.policy.secret_tainted,
            reason=decision.reason,
            request_id=request_id,
        )
        logger.info(
            "Service request needs human approval",
            tool_name=tool_name,
            source_group=source_group,
            short_id=short_id,
            reason=decision.reason,
        )
        # No response file written — container blocks until human decides
        return

    # Script-type MCP: auto-classified as host-mutating → Cop gate
    if not data.get("_cop_approved"):
        from pynchy.security.cop_gate import cop_gate

        s = get_settings()
        mcp_config = getattr(s, "mcp_servers", {}).get(tool_name)
        if mcp_config and mcp_config.type == "script":
            import json as json_mod

            summary = (
                f"script MCP tool: {tool_name}\n"
                f"args: {json_mod.dumps({k: v for k, v in data.items() if k not in ('type', 'request_id', 'source_group')}, default=str)[:1000]}"
            )
            allowed = await cop_gate(
                f"script_mcp:{tool_name}",
                summary,
                data,
                source_group,
                deps,
                request_id=request_id,
            )
            if not allowed:
                return

    # Allowed — record audit and dispatch to plugin handler
    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=tool_name,
        decision="allowed",
        corruption_tainted=gate.policy.corruption_tainted,
        secret_tainted=gate.policy.secret_tainted,
        reason=decision.reason,
        request_id=request_id,
    )

    logger.info(
        "Service request allowed by policy",
        tool_name=tool_name,
        source_group=source_group,
    )

    data["source_group"] = source_group
    response = await handler(data)
    _write_response(source_group, request_id, response)


# Register a prefix handler so all "service:*" IPC types route here.
# The handler itself resolves plugin-provided tool handlers lazily.
register_prefix("service:", _handle_service_request)
