"""Built-in CalDAV MCP server plugin.

Provides host-side handlers for calendar service tools (list_calendar,
list_calendars, create_event, delete_event) backed by CalDAV (e.g. Nextcloud).

Supports multiple named CalDAV servers. Each server has its own credentials
and optional allow/ignore lists for calendar filtering. Calendar names are
auto-discovered from the server; the ``calendar`` parameter accepts either
``calendar_name`` (resolved against the default server) or
``server/calendar_name`` for explicit server selection.

The container-side IPC relay (_tools_calendar.py) sends service requests
through IPC; the host service handler dispatches to these handlers after
policy enforcement.
"""

from __future__ import annotations

import os
from collections.abc import (
    Mapping,  # noqa: TC003 - beartype resolves CalDAV runtime annotations at runtime.
    Sequence,  # noqa: TC003 - beartype resolves CalDAV protocol annotations at runtime.
)
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pluggy

from pynchy.actions.api import ActionId
from pynchy.plugins.api import (
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityRequirement,
    CapabilityRequirementKind,
    HostActionAccess,
    HostActionDescriptor,
    HostActionHandler,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
)
from pynchy.plugins.integrations._service import service_tool

hookimpl = pluggy.HookimplMarker("pynchy")
type _ActionDefinition = tuple[str, str, str, HostActionAccess, HostActionHandler]

# ---------------------------------------------------------------------------
# CalDAV helpers
# ---------------------------------------------------------------------------


_caldav_client_cache: dict[str, object] = {}  # keyed by server name


@dataclass(frozen=True)
class CalDAVServerOptions:
    """Connection and visibility settings for one CalDAV server."""

    url: str
    username: str
    password_env: str | None
    default_calendar: str | None
    allow: tuple[str, ...] | None
    ignore: tuple[str, ...] | None


@dataclass(frozen=True)
class CalDAVRuntime:
    """Resolved CalDAV configuration supplied by the host composition root."""

    default_server: str
    servers: Mapping[str, CalDAVServerOptions]


_runtime: CalDAVRuntime | None = None


def configure_caldav_runtime(runtime: CalDAVRuntime) -> None:
    """Set CalDAV settings before service actions run."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def get_caldav_client(name: str, server_cfg: CalDAVServerOptions) -> object:
    """Get or create a cached DAVClient for a named server."""
    import caldav  # noqa: PLC0415 - optional integration dependency loaded only when CalDAV is used.

    if name not in _caldav_client_cache:
        password = os.environ.get(server_cfg.password_env) if server_cfg.password_env else None
        _caldav_client_cache[name] = caldav.DAVClient(
            url=server_cfg.url,
            username=server_cfg.username,
            password=password,
        )
    return _caldav_client_cache[name]


def clear_caldav_client_cache() -> None:  # noqa: V103
    """Clear cached CalDAV clients (for tests or config reload)."""
    _caldav_client_cache.clear()


def _check_configured(cfg: CalDAVRuntime) -> str | None:
    """Return an error string if no servers are configured, else None."""
    if not cfg.servers:
        return "CalDAV not configured (no servers defined in [tools.caldav.servers.*])"
    return None


def _caldav_config() -> CalDAVRuntime:
    if _runtime is None:
        raise RuntimeError("CalDAV runtime has not been configured")
    return _runtime


def _is_calendar_visible(cal_name: str, server_cfg: CalDAVServerOptions) -> bool:
    """Check whether a calendar passes allow/ignore filtering."""
    lower = cal_name.lower()
    if server_cfg.allow is not None:
        return lower in [a.lower() for a in server_cfg.allow]
    if server_cfg.ignore is not None:
        return lower not in [i.lower() for i in server_cfg.ignore]
    return True


def _filter_calendars(calendars: Sequence[object], server_cfg: CalDAVServerOptions) -> list[object]:
    """Filter a list of CalDAV calendar objects by allow/ignore rules."""
    return [c for c in calendars if c.name and _is_calendar_visible(c.name, server_cfg)]


def _resolve_server(
    cfg: CalDAVRuntime, calendar_str: str | None
) -> tuple[str, CalDAVServerOptions, str | None]:
    """Parse a calendar string and resolve the server.

    Accepts:
      - "server/calendar_name" → explicit server
      - "calendar_name" → default server
      - "primary" or None → default server, default calendar

    Returns (server_name, server_config, calendar_name).
    calendar_name is None when "primary" should be resolved dynamically.
    """
    cal = calendar_str or "primary"

    cal_name: str | None
    if "/" in cal:
        server_name, cal_name = cal.split("/", 1)
    else:
        server_name = cfg.default_server
        cal_name = cal

    if not server_name or server_name not in cfg.servers:
        available = ", ".join(cfg.servers.keys()) or "(none)"
        msg = f"Server '{server_name}' not found. Available: {available}"
        raise ValueError(msg)

    server_cfg = cfg.servers[server_name]

    # "primary" → resolve to server's default_calendar (or None for first-visible)
    if cal_name == "primary":
        cal_name = server_cfg.default_calendar  # may be None

    return server_name, server_cfg, cal_name


def _resolve_calendar(
    server_name: str,
    server_cfg: CalDAVServerOptions,
    calendar_name: str | None,
) -> object:
    """Resolve a calendar object from a specific server.

    If calendar_name is None, returns the first visible calendar.
    Respects allow/ignore filtering — rejects filtered-out calendars.
    """
    client = get_caldav_client(server_name, server_cfg)
    principal = client.principal()
    all_cals = principal.calendars()
    visible = _filter_calendars(all_cals, server_cfg)

    if calendar_name is None:
        # Use first visible calendar
        if not visible:
            msg = f"No visible calendars on server '{server_name}'"
            raise ValueError(msg)
        return visible[0]

    for cal in visible:
        if cal.name and cal.name.lower() == calendar_name.lower():
            return cal

    visible_names = [c.name for c in visible if c.name]
    available = ", ".join(visible_names)
    msg = f"Calendar '{calendar_name}' not found on server '{server_name}'. Available: {available}"
    raise ValueError(msg)


def _parse_event(component: object) -> dict[str, Any]:
    """Extract event fields from an iCalendar VEVENT component."""

    def _get(key: str) -> str | None:
        val = component.get(key)
        if val is None:
            return None
        # datetimes
        if hasattr(val, "dt"):
            dt = val.dt
            if hasattr(dt, "isoformat"):
                return str(dt.isoformat())
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


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------


@service_tool
async def _handle_list_calendars(_data: dict[str, Any]) -> dict[str, Any]:  # noqa: RUF029 - service_tool awaits handlers.
    """Discover all visible calendars across all configured servers."""
    cfg = _caldav_config()
    if err := _check_configured(cfg):
        return {"error": err}

    result: dict[str, list[str]] = {}
    for name, server_cfg in cfg.servers.items():
        client = get_caldav_client(name, server_cfg)
        principal = client.principal()
        all_cals = principal.calendars()
        visible = _filter_calendars(all_cals, server_cfg)
        result[name] = [c.name for c in visible if c.name]

    return {"result": {"servers": result, "default_server": cfg.default_server}}


@service_tool
async def _handle_list_calendar(data: dict[str, Any]) -> dict[str, Any]:  # noqa: RUF029 - service_tool awaits handlers.
    """List calendar events within a date range."""
    cfg = _caldav_config()
    if err := _check_configured(cfg):
        return {"error": err}

    server_name, server_cfg, cal_name = _resolve_server(cfg, data.get("calendar"))
    cal = _resolve_calendar(server_name, server_cfg, cal_name)

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


@service_tool
async def _handle_create_event(data: dict[str, Any]) -> dict[str, Any]:  # noqa: RUF029 - service_tool awaits handlers.
    """Create a calendar event."""
    cfg = _caldav_config()
    if err := _check_configured(cfg):
        return {"error": err}

    server_name, server_cfg, cal_name = _resolve_server(cfg, data.get("calendar"))
    cal = _resolve_calendar(server_name, server_cfg, cal_name)

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


@service_tool
async def _handle_delete_event(data: dict[str, Any]) -> dict[str, Any]:  # noqa: RUF029 - service_tool awaits handlers.
    """Delete a calendar event by UID."""
    cfg = _caldav_config()
    if err := _check_configured(cfg):
        return {"error": err}

    server_name, server_cfg, cal_name = _resolve_server(cfg, data.get("calendar"))
    cal = _resolve_calendar(server_name, server_cfg, cal_name)
    uid = data["event_id"]
    event = cal.event_by_uid(uid)
    event.delete()
    return {"result": {"uid": uid, "status": "deleted"}}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


_CALDAV_ACTIONS: tuple[_ActionDefinition, ...] = (
    (
        "list_calendars",
        "calendar.calendar.list",
        "Discover calendars available through CalDAV.",
        HostActionAccess.READ,
        _handle_list_calendars,
    ),
    (
        "list_calendar",
        "calendar.event.list",
        "List CalDAV events in a date range.",
        HostActionAccess.READ,
        _handle_list_calendar,
    ),
    (
        "create_event",
        "calendar.event.create",
        "Create a CalDAV event.",
        HostActionAccess.WRITE,
        _handle_create_event,
    ),
    (
        "delete_event",
        "calendar.event.delete",
        "Delete a CalDAV event.",
        HostActionAccess.WRITE,
        _handle_delete_event,
    ),
)


def _caldav_action(definition: _ActionDefinition) -> HostActionDescriptor:
    tool_name, action_id, summary, access, handler = definition
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId(action_id),
            kind=CapabilityKind.HOST_ACTION,
            owner="caldav",
            summary=summary,
            action_ids=(ActionId(action_id),),
            requirements=(
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                    name="caldav",
                    description="Enable the CalDAV integration for this workspace.",
                ),
            ),
            documentation="docs/usage/security.md",
        ),
        tool_name=HostToolName(tool_name),
        handler=handler,
        access=access,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(
            IdempotencyMode.NOT_REQUIRED
            if access is HostActionAccess.READ
            else IdempotencyMode.IPC_REQUEST_ID
        ),
        audit=AuditContract(),
        policy_service="caldav",
    )


CALDAV_HOST_ACTIONS = HostActionRegistration(
    actions=tuple(_caldav_action(action) for action in _CALDAV_ACTIONS)
)


class CalDAVMcpServerPlugin:  # noqa: V102
    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return CALDAV_HOST_ACTIONS
