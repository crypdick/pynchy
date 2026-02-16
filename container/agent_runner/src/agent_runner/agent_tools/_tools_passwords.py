"""Password manager tools — search and retrieve credentials via IPC.

These tools write IPC requests that the host processes after applying
policy middleware. Actual password manager integration comes in Step 5.
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from agent_runner.agent_tools._ipc_request import ipc_service_request
from agent_runner.agent_tools._registry import ToolEntry, register

# --- search_passwords ---


def _search_passwords_definition() -> Tool:
    return Tool(
        name="search_passwords",
        description=(
            "Search the password vault. Returns metadata only (titles, URLs), not actual passwords."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (matches against item names and URLs)",
                },
            },
            "required": ["query"],
        },
    )


async def _search_passwords_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request(
        "search_passwords",
        {
            "query": arguments["query"],
        },
    )


# --- get_password ---


def _get_password_definition() -> Tool:
    return Tool(
        name="get_password",
        description=(
            "Retrieve a specific credential from the password vault. "
            "This is a high-risk action that requires human approval."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "ID of the vault item to retrieve",
                },
                "field": {
                    "type": "string",
                    "description": "Which field to retrieve (default: password)",
                    "default": "password",
                    "enum": ["password", "username", "totp", "notes"],
                },
            },
            "required": ["item_id"],
        },
    )


async def _get_password_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request(
        "get_password",
        {
            "item_id": arguments["item_id"],
            "field": arguments.get("field", "password"),
        },
    )


register(
    "search_passwords",
    ToolEntry(definition=_search_passwords_definition, handler=_search_passwords_handle),
)
register(
    "get_password",
    ToolEntry(definition=_get_password_definition, handler=_get_password_handle),
)
