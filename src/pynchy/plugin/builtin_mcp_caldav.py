"""Built-in CalDAV MCP server plugin.

Provides host-side handlers for calendar service tools (list_calendar,
create_event, delete_event) backed by CalDAV (e.g. Nextcloud).

The container-side IPC relay (_tools_calendar.py) sends service requests
through IPC; the host service handler dispatches to these handlers after
policy enforcement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pluggy

from pynchy.config import CalDAVConfig, get_settings
from pynchy.logger import logger

hookimpl = pluggy.HookimplMarker("pynchy")

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


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------


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
# Plugin class
# ---------------------------------------------------------------------------


class CalDAVMcpServerPlugin:
    @hookimpl
    def pynchy_mcp_server_handler(self) -> dict[str, Any]:
        return {
            "tools": {
                "list_calendar": _handle_list_calendar,
                "create_event": _handle_create_event,
                "delete_event": _handle_delete_event,
            },
        }
