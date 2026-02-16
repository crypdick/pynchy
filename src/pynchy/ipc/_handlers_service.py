"""IPC handler for service requests (email, calendar, passwords).

Service requests arrive from container MCP tools with type="service:<tool_name>".
This handler applies the workspace's security policy before processing, then
writes a response file back to the container's responses/ directory.

Calendar operations are backed by CalDAV (Nextcloud). Other services (email,
passwords) are not yet implemented.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pynchy.config import CalDAVConfig, get_settings
from pynchy.ipc._deps import IpcDeps
from pynchy.ipc._registry import register
from pynchy.logger import logger
from pynchy.policy.audit import record_security_event
from pynchy.policy.middleware import PolicyMiddleware
from pynchy.types import McpToolConfig, RateLimitConfig, WorkspaceSecurity

# Cache PolicyMiddleware instances per workspace folder.
# Rebuilt when workspace profiles change (e.g. on reload).
_policy_cache: dict[str, PolicyMiddleware] = {}

# All service tool types that this handler processes
SERVICE_TOOL_TYPES = frozenset(
    {
        "read_email",
        "send_email",
        "list_calendar",
        "create_event",
        "delete_event",
        "search_passwords",
        "get_password",
    }
)


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


def _resolve_security(source_group: str) -> WorkspaceSecurity:
    """Resolve the security profile for a workspace from config.toml.

    config.toml is the source of truth. Falls back to strict defaults
    (all tools require human-approval) if the workspace has no security config.
    """
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
    """Handle a service request with policy enforcement."""
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

    if tool_name not in SERVICE_TOOL_TYPES:
        logger.warning(
            "Unknown service tool type",
            tool_name=tool_name,
            source_group=source_group,
        )
        _write_response(
            source_group,
            request_id,
            {
                "error": f"Unknown service tool: {tool_name}",
            },
        )
        return

    # Resolve workspace security from config.toml
    security = _resolve_security(source_group)
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
                {
                    "error": "Human approval required (TODO: not yet implemented)",
                },
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
                {
                    "error": f"Policy denied: {decision.reason}",
                },
            )

        logger.info(
            "Service request denied by policy",
            tool_name=tool_name,
            source_group=source_group,
            reason=decision.reason,
            requires_approval=decision.requires_approval,
        )
        return

    # Allowed — record audit and process
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

    # Process the allowed request
    await _process_allowed_request(tool_name, data, source_group, request_id)


# ---------------------------------------------------------------------------
# CalDAV helpers
# ---------------------------------------------------------------------------

_caldav_client_cache: dict[str, Any] = {}  # keyed by url


def _get_caldav_client(cfg: CalDAVConfig):
    """Get or create a cached DAVClient instance."""
    import caldav

    key = cfg.url
    if key not in _caldav_client_cache:
        password = cfg.password.get_secret_value() if cfg.password else None
        _caldav_client_cache[key] = caldav.DAVClient(
            url=cfg.url,
            username=cfg.username,
            password=password,
        )
    return _caldav_client_cache[key]


def clear_caldav_client_cache() -> None:
    """Clear cached CalDAV clients (for tests or config reload)."""
    _caldav_client_cache.clear()


def _resolve_calendar(cfg: CalDAVConfig, calendar_name: str | None):
    """Resolve a calendar by name. 'primary' maps to default_calendar."""
    client = _get_caldav_client(cfg)
    principal = client.principal()

    name = calendar_name or "primary"
    if name == "primary":
        name = cfg.default_calendar

    for cal in principal.calendars():
        if cal.name and cal.name.lower() == name.lower():
            return cal

    msg = f"Calendar '{name}' not found"
    raise ValueError(msg)


def _parse_event(component) -> dict:
    """Extract event fields from an iCalendar VEVENT component."""

    def _get(key: str) -> str | None:
        val = component.get(key)
        if val is None:
            return None
        # datetimes
        if hasattr(val, "dt"):
            dt = val.dt
            if hasattr(dt, "isoformat"):
                return dt.isoformat()
            return str(dt)
        return str(val)

    return {
        "uid": _get("uid"),
        "title": _get("summary"),
        "start": _get("dtstart"),
        "end": _get("dtend"),
        "description": _get("description"),
        "location": _get("location"),
    }


async def _handle_list_calendar(data: dict) -> dict:
    """List calendar events within a date range."""
    cfg = get_settings().caldav
    if not cfg.url:
        return {"error": "CalDAV not configured (caldav.url is empty)"}

    try:
        cal = _resolve_calendar(cfg, data.get("calendar"))

        now = datetime.now(tz=UTC)
        start_str = data.get("start_date")
        end_str = data.get("end_date")

        start = datetime.fromisoformat(start_str) if start_str else now
        end = datetime.fromisoformat(end_str) if end_str else now + timedelta(days=7)

        # Ensure timezone-aware
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)

        results = cal.date_search(start=start, end=end, expand=True)

        events = []
        for event_obj in results:
            component = event_obj.icalendar_component
            if component:
                events.append(_parse_event(component))

        return {"result": {"events": events, "count": len(events)}}
    except Exception as e:
        logger.error("CalDAV list_calendar failed", error=str(e))
        return {"error": f"CalDAV error: {e}"}


async def _handle_create_event(data: dict) -> dict:
    """Create a calendar event."""
    cfg = get_settings().caldav
    if not cfg.url:
        return {"error": "CalDAV not configured (caldav.url is empty)"}

    try:
        cal = _resolve_calendar(cfg, data.get("calendar"))

        ical_kwargs: dict[str, Any] = {}
        ical_kwargs["dtstart"] = datetime.fromisoformat(data["start"])
        ical_kwargs["dtend"] = datetime.fromisoformat(data["end"])
        ical_kwargs["summary"] = data["title"]

        if data.get("description"):
            ical_kwargs["description"] = data["description"]
        if data.get("location"):
            ical_kwargs["location"] = data["location"]

        event = cal.save_event(**ical_kwargs)

        uid = None
        component = event.icalendar_component
        if component:
            uid_val = component.get("uid")
            if uid_val:
                uid = str(uid_val)

        return {"result": {"uid": uid, "status": "created"}}
    except Exception as e:
        logger.error("CalDAV create_event failed", error=str(e))
        return {"error": f"CalDAV error: {e}"}


async def _handle_delete_event(data: dict) -> dict:
    """Delete a calendar event by UID."""
    cfg = get_settings().caldav
    if not cfg.url:
        return {"error": "CalDAV not configured (caldav.url is empty)"}

    try:
        cal = _resolve_calendar(cfg, data.get("calendar"))
        uid = data["event_id"]
        event = cal.event_by_uid(uid)
        event.delete()
        return {"result": {"uid": uid, "status": "deleted"}}
    except Exception as e:
        logger.error("CalDAV delete_event failed", error=str(e))
        return {"error": f"CalDAV error: {e}"}


# ---------------------------------------------------------------------------
# Service dispatch
# ---------------------------------------------------------------------------

_SERVICE_HANDLERS: dict[str, Callable[[dict], Awaitable[dict]]] = {
    "list_calendar": _handle_list_calendar,
    "create_event": _handle_create_event,
    "delete_event": _handle_delete_event,
}


async def _process_allowed_request(
    tool_name: str,
    data: dict,
    source_group: str,
    request_id: str,
) -> None:
    """Process an allowed service request by dispatching to the appropriate handler."""
    handler = _SERVICE_HANDLERS.get(tool_name)
    if handler:
        response = await handler(data)
    else:
        response = {
            "error": (
                f"Service '{tool_name}' is not implemented yet. "
                f"The request was approved by policy, but no backend is connected."
            ),
        }
    _write_response(source_group, request_id, response)


# Register handlers for all service tool types.
# The IPC type is "service:<tool_name>" (e.g. "service:read_email").
for _tool_type in SERVICE_TOOL_TYPES:
    register(f"service:{_tool_type}", _handle_service_request)
