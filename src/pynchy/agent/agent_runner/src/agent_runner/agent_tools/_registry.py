"""Tool registry for the agent MCP server."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool


@dataclass
class ToolEntry:
    """A registered tool with its definition and handler."""

    definition: Callable[[], Tool | None]
    handler: Callable[..., Awaitable[list[TextContent] | CallToolResult]]


_TOOLS: dict[str, ToolEntry] = {}


def register(name: str, entry: ToolEntry) -> None:
    """Register a tool by name."""
    _TOOLS[name] = entry


def register_ipc_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
) -> None:
    """Register a tool that forwards arguments to an IPC service request.

    Generates both the Tool definition and an async handler that calls
    ``ipc_service_request(name, arguments)``, applying any ``default``
    values declared in the input schema for fields the caller omits.

    Use this for tools that are pure IPC proxies — no local validation
    or custom logic, just forwarding to the host.
    """
    from agent_runner.agent_tools._ipc_request import ipc_service_request

    # Pre-compute defaults from schema so the handler closure is fast.
    defaults: dict[str, Any] = {}
    for prop_name, prop_def in input_schema.get("properties", {}).items():
        if "default" in prop_def:
            defaults[prop_name] = prop_def["default"]

    async def handler(arguments: dict) -> list[TextContent]:
        request = {**defaults, **arguments}
        return await ipc_service_request(name, request)

    register(
        name,
        ToolEntry(
            definition=lambda: Tool(name=name, description=description, inputSchema=input_schema),
            handler=handler,
        ),
    )


def all_tools() -> list[Tool]:
    """Return all visible tool definitions (None = hidden for this context)."""
    return [t for e in _TOOLS.values() if (t := e.definition()) is not None]


def tool_error(msg: str) -> CallToolResult:
    """Return an MCP error result with a text message."""
    return CallToolResult(
        content=[TextContent(type="text", text=msg)],
        isError=True,
    )


def get_handler(name: str) -> Callable[..., Awaitable[list[TextContent] | CallToolResult]] | None:
    """Look up the handler for a tool name."""
    entry = _TOOLS.get(name)
    return entry.handler if entry else None
