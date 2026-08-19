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
    "_tools_vaultwarden",
    "_tools_x",
)

_TOOLS_BY_AGENT_GRANT = {
    "caldav": frozenset({"list_calendars", "list_calendar", "create_event", "delete_event"}),
    "computer_use": frozenset({"computer_use"}),
    "gog": frozenset(
        {
            "gog_setup_start",
            "gog_setup_complete",
            "gog_gmail_search",
            "gog_gmail_get",
            "gog_gmail_create_draft",
            "gog_gmail_send_draft",
            "gog_gmail_send",
            "gog_contacts_search",
            "gog_docs_read",
            "gog_docs_export",
            "gog_sheets_get",
            "gog_sheets_update",
        }
    ),
    "linear": frozenset(
        {
            "linear_submit_plan",
            "linear_create_comment",
            "linear_list_work_items",
            "linear_reconcile_work_item",
            "linear_move_todo",
        }
    ),
    "marketplace-health": frozenset({"marketplace_health_snapshot"}),
    "vaultwarden": frozenset({"get_secret"}),
    "vaultwarden-admin": frozenset({"manage_vaultwarden"}),
    "matrix_route_read": frozenset({"matrix_route_read"}),
    "matrix_route_send": frozenset({"matrix_route_send"}),
    "slack_token_extractor": frozenset({"setup_slack_session", "refresh_slack_tokens"}),
    "x_integration": frozenset(
        {"setup_x_session", "x_post", "x_like", "x_reply", "x_retweet", "x_quote"}
    ),
}
_GRANT_GATED_TOOLS = frozenset().union(*_TOOLS_BY_AGENT_GRANT.values())

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


def enabled_agent_tools(agent_tool_grants: list[str]) -> list[str]:
    """Return core tools plus tool families granted to one workspace."""
    granted = frozenset().union(
        *(_TOOLS_BY_AGENT_GRANT.get(name, frozenset()) for name in agent_tool_grants)
    )
    return [
        tool.name
        for tool in all_tools()
        if tool.name not in _GRANT_GATED_TOOLS or tool.name in granted
    ]


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
