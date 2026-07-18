"""Matrix communications tools backed by Pynchy's host-only gateway."""

from ._registry import register_ipc_tool

register_ipc_tool(
    name="matrix_list_chats",
    description="List chats available through the owner's private Matrix gateway.",
    input_schema={"type": "object", "properties": {}},
)

register_ipc_tool(
    name="matrix_list_messages",
    description="Read recent text messages from one Matrix chat without changing it.",
    input_schema={
        "type": "object",
        "properties": {
            "room_id": {"type": "string", "description": "Room ID from matrix_list_chats."},
            "limit": {
                "type": "integer",
                "description": "Number of messages to return (1-250).",
                "default": 50,
                "minimum": 1,
                "maximum": 250,
            },
        },
        "required": ["room_id"],
    },
)

register_ipc_tool(
    name="matrix_send_message",
    description=(
        "Send a plain-text message as the Matrix gateway owner. The recipient sees the "
        "owner's bridged account, not Pynchy. This external action requires approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "room_id": {"type": "string", "description": "Room ID from matrix_list_chats."},
            "body": {"type": "string", "description": "Final approved plain-text message."},
        },
        "required": ["room_id", "body"],
    },
)
