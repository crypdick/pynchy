"""Calendar tools — list, create, and delete events via IPC service requests.

Supports multiple CalDAV servers. The ``calendar`` parameter accepts either
``calendar_name`` (resolved against the default server) or
``server/calendar_name`` for explicit server selection. Use ``list_calendars``
to discover available servers and calendars.

These tools write IPC requests that the host processes after applying
policy middleware.
"""

from agent_runner.agent_tools._registry import register_ipc_tool

register_ipc_tool(
    name="list_calendars",
    description=(
        "Discover all available calendars across all configured CalDAV servers. "
        "Returns server names and their visible calendars. Use this to find out "
        "what calendars are available before using other calendar tools."
    ),
    input_schema={
        "type": "object",
        "properties": {},
    },
)

register_ipc_tool(
    name="list_calendar",
    description="List calendar events within a date range.",
    input_schema={
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

register_ipc_tool(
    name="create_event",
    description="Create a calendar event.",
    input_schema={
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

register_ipc_tool(
    name="delete_event",
    description=(
        "Delete a calendar event. This is a destructive action that may require human approval."
    ),
    input_schema={
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
