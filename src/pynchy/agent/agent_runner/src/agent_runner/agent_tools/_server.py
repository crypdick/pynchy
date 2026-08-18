"""MCP server setup for agent tools.

Discovers tools from the registry instead of hardcoding them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from importlib import import_module
from typing import Any, cast

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ._registry import HandlerResult, all_tools, get_handler

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
    "_tools_marketplace_health",
    "_tools_messaging",
    "_tools_slack_tokens",
    "_tools_skills",
    "_tools_tasks",
    "_tools_todos",
    "_tools_x",
)

for module_name in _TOOL_MODULES:
    import_module(f"{__package__}.{module_name}")

_INSTRUCTIONS = (
    "Before improvising a workflow that may have personalized guidance, call search_skills with "
    "task terms. If it returns a relevant skill, call request_skill_access before proceeding. Use "
    "Pynchy MCP tools directly; do not invoke them through Bash or shell wrappers."
)

server = Server("pynchy", instructions=_INSTRUCTIONS)

type ListToolsHandler = Callable[[], Awaitable[list[Tool]]]
type ListToolsDecorator = Callable[[ListToolsHandler], ListToolsHandler]

# mcp 1.26 leaves Server.list_tools() untyped. Its runtime contract returns the
# decorated function unchanged, so contain the missing third-party type here.
_list_tools_decorator = cast("Callable[[], ListToolsDecorator]", server.list_tools)


# mcp's Server.list_tools()/call_tool() decorators are themselves untyped, so
# mypy flags the wrapped handlers as untyped-decorator; nothing in our code
# consumes these functions directly (the server registers them).
@_list_tools_decorator()
async def list_tools() -> list[Tool]:  # noqa: RUF029 - async MCP callback API.
    return all_tools()


@server.call_tool()  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> HandlerResult:
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
