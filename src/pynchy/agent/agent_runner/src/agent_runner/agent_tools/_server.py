"""MCP server setup for agent tools.

Discovers tools from the registry instead of hardcoding them.
"""

from __future__ import annotations

from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

# Import tool modules to trigger self-registration
import agent_runner.agent_tools._tools_admin
import agent_runner.agent_tools._tools_ask_user
import agent_runner.agent_tools._tools_calendar
import agent_runner.agent_tools._tools_computer_use
import agent_runner.agent_tools._tools_desktop_screenshot
import agent_runner.agent_tools._tools_google_setup
import agent_runner.agent_tools._tools_lifecycle
import agent_runner.agent_tools._tools_memory
import agent_runner.agent_tools._tools_messaging
import agent_runner.agent_tools._tools_slack_tokens
import agent_runner.agent_tools._tools_tasks
import agent_runner.agent_tools._tools_todos
import agent_runner.agent_tools._tools_x
from agent_runner.agent_tools._registry import all_tools, get_handler

server = Server("pynchy")


# mcp's Server.list_tools()/call_tool() decorators are themselves untyped, so
# mypy flags the wrapped handlers as untyped-decorator; nothing in our code
# consumes these functions directly (the server registers them).
@server.list_tools()  # type: ignore[untyped-decorator]
async def list_tools() -> list[Tool]:
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
