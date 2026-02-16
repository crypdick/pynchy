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

from datetime import UTC, datetime, timedelta
from typing import Any

import pluggy

from pynchy.config import CalDAVConfig, CalDAVServerConfig, get_settings
from pynchy.logger import logger

hookimpl = pluggy.HookimplMarker("pynchy")

# ---------------------------------------------------------------------------
# CalDAV helpers
# ---------------------------------------------------------------------------

_caldav_client_cache: dict[str, Any] = {}  # keyed by server name


def _get_caldav_client(name: str, server_cfg: CalDAVServerConfig):
    """Get or create a cached DAVClient for a named server."""
    import caldav

    if name not in _caldav_client_cache:
        password = server_cfg.password.get_secret_value() if server_cfg.password else None
        _caldav_client_cache[name] = caldav.DAVClient(
            url=server_cfg.url,
            username=server_cfg.username,
            password=password,
        )
    return _caldav_client_cache[name]


def clear_caldav_client_cache() -> None:
    """Clear cached CalDAV clients (for tests or config reload)."""
    _caldav_client_cache.clear()


def _check_configured(cfg: CalDAVConfig) -> str | None:
    """Return an error string if no servers are configured, else None."""
    if not cfg.servers:
        return "CalDAV not configured (no servers defined in [caldav.servers.*])"
    return None


def _is_calendar_visible(cal_name: str, server_cfg: CalDAVServerConfig) -> bool:
    """Check whether a calendar passes allow/ignore filtering."""
    lower = cal_name.lower()
    if server_cfg.allow is not None:
        return lower in [a.lower() for a in server_cfg.allow]
    if server_cfg.ignore is not None:
        return lower not in [i.lower() for i in server_cfg.ignore]
    return True


def _filter_calendars(calendars: list, server_cfg: CalDAVServerConfig) -> list:
    """Filter a list of CalDAV calendar objects by allow/ignore rules."""
    return [c for c in calendars if c.name and _is_calendar_visible(c.name, server_cfg)]


def _resolve_server(
    cfg: CalDAVConfig, calendar_str: str | None
) -> tuple[str, CalDAVServerConfig, str | None]:
    """Parse a calendar string and resolve the server.

    Accepts:
      - "server/calendar_name" → explicit server
      - "calendar_name" → default server
      - "primary" or None → default server, default calendar

    Returns (server_name, server_config, calendar_name).
    calendar_name is None when "primary" should be resolved dynamically.
    """
    cal = calendar_str or "primary"

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
    server_name: str, server_cfg: CalDAVServerConfig, calendar_name: str | None
):
    """Resolve a calendar object from a specific server.

    If calendar_name is None, returns the first visible calendar.
    Respects allow/ignore filtering — rejects filtered-out calendars.
    """
    client = _get_caldav_client(server_name, server_cfg)
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


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------


async def _handle_list_calendars(data: dict) -> dict:
    """Discover all visible calendars across all configured servers."""
    cfg = get_settings().caldav
    if err := _check_configured(cfg):
        return {"error": err}

    try:
        result: dict[str, list[str]] = {}
        for name, server_cfg in cfg.servers.items():
            client = _get_caldav_client(name, server_cfg)
            principal = client.principal()
            all_cals = principal.calendars()
            visible = _filter_calendars(all_cals, server_cfg)
            result[name] = [c.name for c in visible if c.name]

        return {"result": {"servers": result, "default_server": cfg.default_server}}
    except Exception as e:
        logger.error("CalDAV list_calendars failed", error=str(e))
        return {"error": f"CalDAV error: {e}"}


async def _handle_list_calendar(data: dict) -> dict:
    """List calendar events within a date range."""
    cfg = get_settings().caldav
    if err := _check_configured(cfg):
        return {"error": err}

    try:
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
    except Exception as e:
        logger.error("CalDAV list_calendar failed", error=str(e))
        return {"error": f"CalDAV error: {e}"}


async def _handle_create_event(data: dict) -> dict:
    """Create a calendar event."""
    cfg = get_settings().caldav
    if err := _check_configured(cfg):
        return {"error": err}

    try:
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
    except Exception as e:
        logger.error("CalDAV create_event failed", error=str(e))
        return {"error": f"CalDAV error: {e}"}


async def _handle_delete_event(data: dict) -> dict:
    """Delete a calendar event by UID."""
    cfg = get_settings().caldav
    if err := _check_configured(cfg):
        return {"error": err}

    try:
        server_name, server_cfg, cal_name = _resolve_server(cfg, data.get("calendar"))
        cal = _resolve_calendar(server_name, server_cfg, cal_name)
        uid = data["event_id"]
        event = cal.event_by_uid(uid)
        event.delete()
        return {"result": {"uid": uid, "status": "deleted"}}
    except Exception as e:
        logger.error("CalDAV delete_event failed", error=str(e))
        return {"error": f"CalDAV error: {e}"}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class CalDAVMcpServerPlugin:
    @hookimpl
    def pynchy_mcp_server_handler(self) -> dict[str, Any]:
        return {
            "tools": {
                "list_calendars": _handle_list_calendars,
                "list_calendar": _handle_list_calendar,
                "create_event": _handle_create_event,
                "delete_event": _handle_delete_event,
            },
        }
