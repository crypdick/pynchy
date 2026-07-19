"""MCP server setup for agent tools.

Discovers tools from the registry instead of hardcoding them.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from ._registry import all_tools, get_handler

_TOOL_MODULES = (
    "_tools_admin",
    "_tools_ask_user",
    "_tools_calendar",
    "_tools_computer_use",
    "_tools_desktop_screenshot",
    "_tools_gog",
    "_tools_google_setup",
    "_tools_lifecycle",
    "_tools_linear",
    "_tools_matrix",
    "_tools_memory",
    "_tools_messaging",
    "_tools_slack_tokens",
    "_tools_skills",
    "_tools_tasks",
    "_tools_todos",
    "_tools_x",
)

for module_name in _TOOL_MODULES:
    import_module(f"{__package__}.{module_name}")

server = Server("pynchy")


# mcp's Server.list_tools()/call_tool() decorators are themselves untyped, so
# mypy flags the wrapped handlers as untyped-decorator; nothing in our code
# consumes these functions directly (the server registers them).
@server.list_tools()  # type: ignore[untyped-decorator]
async def list_tools() -> list[Tool]:  # noqa: RUF029, RUF100 - async MCP callback API.
    return all_tools()


@server.call_tool()  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent] | CallToolResult:
    handler = get_handler(name)
    if handler:
        return await handler(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def run_server() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
