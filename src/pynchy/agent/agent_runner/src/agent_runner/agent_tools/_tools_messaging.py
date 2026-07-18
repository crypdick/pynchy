"""send_message tool."""

from __future__ import annotations

from typing import Any

from mcp.types import TextContent

from . import _ipc
from ._registry import tool


@tool(
    "send_message",
    (
        "Send a message to the user or group immediately while "
        "you're still running. Use this for progress updates or "
        "to send multiple messages. You can call this multiple "
        "times. Note: when running as a scheduled task, your "
        "final output is NOT sent to the user — use this tool "
        "if you need to communicate with the user or group."
    ),
    {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The message text to send",
            },
            "sender": {
                "type": "string",
                "description": (
                    'Your role/identity name (e.g. "Researcher"). '
                    "When set, messages appear from a dedicated "
                    "bot in Telegram."
                ),
            },
        },
        "required": ["text"],
    },
)
async def _handle(  # noqa: RUF029, RUF100 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent]:
    data = {
        "type": "message",
        "chatJid": _ipc.get_agent_tool_runtime().chat_jid,
        "text": arguments["text"],
        "groupFolder": _ipc.get_agent_tool_runtime().group_folder,
        "timestamp": _ipc.now_iso(),
    }
    if arguments.get("sender"):
        data["sender"] = arguments["sender"]

    _ipc.write_ipc_file(_ipc.get_agent_tool_runtime().ipc_dir / "messages", data)
    return [TextContent(type="text", text="Message sent.")]
