"""send_message tool."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from agent_runner.agent_tools import _ipc
from agent_runner.agent_tools._registry import ToolEntry, register


def _definition() -> Tool:
    return Tool(
        name="send_message",
        description=(
            "Send a message to the user or group immediately while "
            "you're still running. Use this for progress updates or "
            "to send multiple messages. You can call this multiple "
            "times. Note: when running as a scheduled task, your "
            "final output is NOT sent to the user — use this tool "
            "if you need to communicate with the user or group."
        ),
        inputSchema={
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


async def _handle(arguments: dict) -> list[TextContent]:
    data = {
        "type": "message",
        "chatJid": _ipc.chat_jid,
        "text": arguments["text"],
        "groupFolder": _ipc.group_folder,
        "timestamp": _ipc.now_iso(),
    }
    if arguments.get("sender"):
        data["sender"] = arguments["sender"]

    _ipc.write_ipc_file(_ipc.MESSAGES_DIR, data)
    return [TextContent(type="text", text="Message sent.")]


register("send_message", ToolEntry(definition=_definition, handler=_handle))
