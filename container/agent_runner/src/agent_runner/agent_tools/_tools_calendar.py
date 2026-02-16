"""Calendar tools — list, create, and delete events via IPC service requests.

Supports multiple CalDAV servers. The ``calendar`` parameter accepts either
``calendar_name`` (resolved against the default server) or
``server/calendar_name`` for explicit server selection. Use ``list_calendars``
to discover available servers and calendars.

These tools write IPC requests that the host processes after applying
policy middleware.
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from agent_runner.agent_tools._ipc_request import ipc_service_request
from agent_runner.agent_tools._registry import ToolEntry, register

# --- list_calendars ---


def _list_calendars_definition() -> Tool:
    return Tool(
        name="list_calendars",
        description=(
            "Discover all available calendars across all configured CalDAV servers. "
            "Returns server names and their visible calendars. Use this to find out "
            "what calendars are available before using other calendar tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    )


async def _list_calendars_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request("list_calendars", {})


# --- list_calendar ---


def _list_calendar_definition() -> Tool:
    return Tool(
        name="list_calendar",
        description="List calendar events within a date range.",
        inputSchema={
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start date in ISO format (default: today)",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in ISO format (default: 7 days from now)",
                },
                "calendar": {
                    "type": "string",
                    "description": (
                        'Calendar name (default: primary). Use "server/calendar" '
                        "to target a specific server, or just "
                        '"calendar" for the default server.'
                    ),
                    "default": "primary",
                },
            },
        },
    )


async def _list_calendar_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request(
        "list_calendar",
        {
            "start_date": arguments.get("start_date"),
            "end_date": arguments.get("end_date"),
            "calendar": arguments.get("calendar", "primary"),
        },
    )


# --- create_event ---


def _create_event_definition() -> Tool:
    return Tool(
        name="create_event",
        description="Create a calendar event.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Event title",
                },
                "start": {
                    "type": "string",
                    "description": "Start datetime in ISO format",
                },
                "end": {
                    "type": "string",
                    "description": "End datetime in ISO format",
                },
                "description": {
                    "type": "string",
                    "description": "Event description",
                },
                "location": {
                    "type": "string",
                    "description": "Event location",
                },
                "calendar": {
                    "type": "string",
                    "description": (
                        'Calendar name (default: primary). Use "server/calendar" '
                        "to target a specific server, or just "
                        '"calendar" for the default server.'
                    ),
                    "default": "primary",
                },
            },
            "required": ["title", "start", "end"],
        },
    )


async def _create_event_handle(arguments: dict) -> list[TextContent]:
    request = {
        "title": arguments["title"],
        "start": arguments["start"],
        "end": arguments["end"],
        "calendar": arguments.get("calendar", "primary"),
    }
    if arguments.get("description"):
        request["description"] = arguments["description"]
    if arguments.get("location"):
        request["location"] = arguments["location"]
    return await ipc_service_request("create_event", request)


# --- delete_event ---


def _delete_event_definition() -> Tool:
    return Tool(
        name="delete_event",
        description=(
            "Delete a calendar event. This is a destructive action that may require human approval."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "ID of the event to delete",
                },
                "calendar": {
                    "type": "string",
                    "description": (
                        'Calendar name (default: primary). Use "server/calendar" '
                        "to target a specific server, or just "
                        '"calendar" for the default server.'
                    ),
                    "default": "primary",
                },
            },
            "required": ["event_id"],
        },
    )


async def _delete_event_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request(
        "delete_event",
        {
            "event_id": arguments["event_id"],
            "calendar": arguments.get("calendar", "primary"),
        },
    )


register(
    "list_calendars",
    ToolEntry(definition=_list_calendars_definition, handler=_list_calendars_handle),
)
register(
    "list_calendar",
    ToolEntry(definition=_list_calendar_definition, handler=_list_calendar_handle),
)
register(
    "create_event",
    ToolEntry(definition=_create_event_definition, handler=_create_event_handle),
)
register(
    "delete_event",
    ToolEntry(definition=_delete_event_definition, handler=_delete_event_handle),
)
