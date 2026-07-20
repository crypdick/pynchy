"""Matrix tools scoped by the active routed conversation on the host."""

from ._registry import register_ipc_tool

register_ipc_tool(
    name="matrix_route_read",
    description=(
        "Read recent text messages from this conversation's configured Matrix route. "
        "The host chooses the room; no destination can be supplied."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Number of messages to return (1-250).",
                "default": 50,
                "minimum": 1,
                "maximum": 250,
            }
        },
    },
)

register_ipc_tool(
    name="matrix_route_send",
    description=(
        "Request an exact plain-text reply on this conversation's configured Matrix route. "
        "The host chooses and rechecks the destination; every send requires human approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": "Final plain-text message."},
        },
        "required": ["body"],
    },
)
