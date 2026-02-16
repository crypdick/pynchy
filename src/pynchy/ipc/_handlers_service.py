"""IPC handler for service requests dispatched to MCP server plugins.

Service requests arrive from container MCP tools with type="service:<tool_name>".
This handler applies the workspace's security policy, then dispatches to
plugin-provided handlers discovered via the ``pynchy_mcp_server_handler`` hook.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pynchy.config import get_settings
from pynchy.ipc._deps import IpcDeps
from pynchy.ipc._registry import register_prefix
from pynchy.logger import logger
from pynchy.plugin import get_plugin_manager
from pynchy.policy.audit import record_security_event
from pynchy.policy.middleware import PolicyMiddleware
from pynchy.types import McpToolConfig, RateLimitConfig, WorkspaceSecurity

# Cache PolicyMiddleware instances per workspace folder.
# Rebuilt when workspace profiles change (e.g. on reload).
_policy_cache: dict[str, PolicyMiddleware] = {}

# Lazily populated mapping of tool_name → async handler from plugins.
_plugin_handlers: dict[str, Callable[[dict], Awaitable[dict]]] | None = None


def _get_plugin_handlers() -> dict[str, Callable[[dict], Awaitable[dict]]]:
    """Collect and cache tool handlers from all MCP server plugins."""
    global _plugin_handlers  # noqa: PLW0603
    if _plugin_handlers is not None:
        return _plugin_handlers

    pm = get_plugin_manager()
    merged: dict[str, Callable[[dict], Awaitable[dict]]] = {}
    for result in pm.hook.pynchy_mcp_server_handler():
        tools = result.get("tools", {})
        merged.update(tools)

    _plugin_handlers = merged
    return _plugin_handlers


def clear_plugin_handler_cache() -> None:
    """Clear the cached plugin handler mapping (for tests or config reload)."""
    global _plugin_handlers  # noqa: PLW0603
    _plugin_handlers = None


def _get_policy(source_group: str, security: WorkspaceSecurity) -> PolicyMiddleware:
    """Get or create a PolicyMiddleware for a workspace."""
    if source_group not in _policy_cache:
        _policy_cache[source_group] = PolicyMiddleware(security)
    return _policy_cache[source_group]


def clear_policy_cache() -> None:
    """Clear cached PolicyMiddleware instances (e.g. on config reload)."""
    _policy_cache.clear()


def _write_response(source_group: str, request_id: str, response: dict) -> None:
    """Write a response file for the container to pick up."""
    s = get_settings()
    responses_dir = s.data_dir / "ipc" / source_group / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)

    filepath = responses_dir / f"{request_id}.json"
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(response, indent=2))
    temp_path.rename(filepath)


def _resolve_security(source_group: str, *, is_god: bool = False) -> WorkspaceSecurity:
    """Resolve the security profile for a workspace from config.toml.

    config.toml is the source of truth. Falls back to strict defaults
    (all tools require human-approval) if the workspace has no security config.

    God workspaces auto-approve all tools since they are fully trusted.
    """
    # God workspace is fully trusted — skip policy gates.
    # TODO: Re-evaluate when human-approval gate is implemented
    #   (backlog/2-planning/security-hardening-6-approval.md).
    if is_god:
        return WorkspaceSecurity(default_risk_tier="always-approve")

    s = get_settings()
    ws_config = s.workspaces.get(source_group)

    if ws_config is None or ws_config.security is None:
        return WorkspaceSecurity()

    sec = ws_config.security

    mcp_tools = {
        name: McpToolConfig(risk_tier=tool.risk_tier, enabled=tool.enabled)
        for name, tool in sec.mcp_tools.items()
    }

    rate_limits = None
    if sec.rate_limits is not None:
        rate_limits = RateLimitConfig(
            max_calls_per_hour=sec.rate_limits.max_calls_per_hour,
            per_tool_overrides=sec.rate_limits.per_tool_overrides,
        )

    return WorkspaceSecurity(
        mcp_tools=mcp_tools,
        default_risk_tier=sec.default_risk_tier,
        rate_limits=rate_limits,
    )


async def _handle_service_request(
    data: dict[str, Any],
    source_group: str,
    is_god: bool,
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

    # Resolve workspace security from config.toml
    security = _resolve_security(source_group, is_god=is_god)
    policy = _get_policy(source_group, security)

    # Find the chat_jid for this group (for audit logging)
    chat_jid = "unknown"
    for jid, group in deps.registered_groups().items():
        if group.folder == source_group:
            chat_jid = jid
            break

    # Evaluate policy
    decision = policy.evaluate(tool_name, data)

    # Determine tier for audit log
    tool_config = security.mcp_tools.get(tool_name)
    tier = tool_config.risk_tier if tool_config else security.default_risk_tier

    if not decision.allowed:
        if decision.requires_approval:
            await record_security_event(
                chat_jid=chat_jid,
                workspace=source_group,
                tool_name=tool_name,
                decision="approval_requested",
                tier=tier,
                reason=decision.reason,
                request_id=request_id,
            )
            _write_response(
                source_group,
                request_id,
                {"error": "Human approval required (TODO: not yet implemented)"},
            )
        else:
            audit_decision = (
                "rate_limited" if "rate limit" in (decision.reason or "").lower() else "denied"
            )
            await record_security_event(
                chat_jid=chat_jid,
                workspace=source_group,
                tool_name=tool_name,
                decision=audit_decision,
                tier=tier,
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
            requires_approval=decision.requires_approval,
        )
        return

    # Allowed — record audit and dispatch to plugin handler
    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=tool_name,
        decision="allowed",
        tier=tier,
        reason=decision.reason,
        request_id=request_id,
    )

    logger.info(
        "Service request allowed by policy",
        tool_name=tool_name,
        source_group=source_group,
        tier=tier,
    )

    response = await handler(data)
    _write_response(source_group, request_id, response)


# Register a prefix handler so all "service:*" IPC types route here.
# The handler itself resolves plugin-provided tool handlers lazily.
register_prefix("service:", _handle_service_request)
