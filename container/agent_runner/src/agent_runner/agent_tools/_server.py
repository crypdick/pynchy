"""MCP server setup for agent tools.

Discovers tools from the registry instead of hardcoding them.
"""

from __future__ import annotations

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

# Import tool modules to trigger self-registration
import agent_runner.agent_tools._tools_admin  # noqa: F401
import agent_runner.agent_tools._tools_calendar  # noqa: F401
import agent_runner.agent_tools._tools_lifecycle  # noqa: F401
import agent_runner.agent_tools._tools_memory  # noqa: F401
import agent_runner.agent_tools._tools_messaging  # noqa: F401
import agent_runner.agent_tools._tools_slack_tokens  # noqa: F401
import agent_runner.agent_tools._tools_tasks  # noqa: F401
import agent_runner.agent_tools._tools_todos  # noqa: F401
import agent_runner.agent_tools._tools_x  # noqa: F401
from agent_runner.agent_tools._registry import all_tools, get_handler

server = Server("pynchy")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return all_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent] | CallToolResult:
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
