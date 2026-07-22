"""Messaging tools."""

from __future__ import annotations

from typing import Any

from mcp.types import TextContent

from . import _ipc
from ._ipc_request import ipc_service_request
from ._registry import tool

_PERSONAL_MESSAGING_SOURCES = ["whatsapp", "signal", "google_messages"]


@tool(
    "messaging_source_health",
    (
        "Read current health and latest inbound timestamps for Pynchy messaging runtimes "
        "and configured host-local aggregate sources. Use this instead of inspecting host "
        "environment variables, invoking sibling messaging utilities, or opening provider "
        "applications. This tool never reads sender identities or message bodies, opens "
        "conversations, or changes provider read state. Its latest_inbound field identifies "
        "the newest body-free timestamp across returned sources. When sources is omitted, "
        "the tool checks only WhatsApp, Signal, and Google Messages; pass a configured "
        "connection name or provider explicitly to inspect another Pynchy channel."
    ),
    {
        "type": "object",
        "properties": {
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "default": _PERSONAL_MESSAGING_SOURCES,
                "description": (
                    "Optional connection names or provider types to inspect, such as "
                    "whatsapp, signal, google_messages, or discord. Omit to inspect only "
                    "the three personal messaging sources."
                ),
            }
        },
    },
)
async def _messaging_source_health_handle(arguments: dict[str, Any]) -> list[TextContent]:
    sources = arguments.get("sources", _PERSONAL_MESSAGING_SOURCES)
    return await ipc_service_request(
        "messaging_source_health",
        {"sources": sources},
        type_override="messaging_source_health",
    )


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
