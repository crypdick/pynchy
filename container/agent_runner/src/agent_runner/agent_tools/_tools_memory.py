"""Memory tools — save, recall, forget, and list memories via IPC service requests.

These tools provide persistent, searchable memory across sessions.
Memories are stored per-workspace with BM25-ranked full-text search.
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from agent_runner.agent_tools._ipc_request import ipc_service_request
from agent_runner.agent_tools._registry import ToolEntry, register

# --- save_memory ---


def _save_memory_definition() -> Tool:
    return Tool(
        name="save_memory",
        description=(
            "Save a fact, preference, or note to persistent memory. "
            "Use a descriptive key (e.g., 'user-favorite-color', 'project-deadline'). "
            "If the key already exists, the content is updated."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": (
                        "Unique identifier for this memory (e.g., 'user-favorite-color')"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The information to remember",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Category: 'core' (permanent facts, default), "
                        "'daily' (session context), 'conversation' (auto-archived)"
                    ),
                    "default": "core",
                },
            },
            "required": ["key", "content"],
        },
    )


async def _save_memory_handle(arguments: dict) -> list[TextContent]:
    request = {
        "key": arguments["key"],
        "content": arguments["content"],
    }
    if arguments.get("category"):
        request["category"] = arguments["category"]
    return await ipc_service_request("save_memory", request)


# --- recall_memories ---


def _recall_memories_definition() -> Tool:
    return Tool(
        name="recall_memories",
        description=(
            "Search memories by keyword. Returns the most relevant matches "
            "ranked by BM25 relevance scoring. Use this to recall previously "
            "saved facts, preferences, or context."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords (e.g., 'favorite color', 'project deadline')",
                },
                "category": {
                    "type": "string",
                    "description": "Filter by category (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    )


async def _recall_memories_handle(arguments: dict) -> list[TextContent]:
    request = {"query": arguments["query"]}
    if arguments.get("category"):
        request["category"] = arguments["category"]
    if arguments.get("limit"):
        request["limit"] = arguments["limit"]
    return await ipc_service_request("recall_memories", request)


# --- forget_memory ---


def _forget_memory_definition() -> Tool:
    return Tool(
        name="forget_memory",
        description="Delete a memory by key. Use this to remove outdated or incorrect information.",
        inputSchema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key of the memory to remove",
                },
            },
            "required": ["key"],
        },
    )


async def _forget_memory_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request("forget_memory", {"key": arguments["key"]})


# --- list_memories ---


def _list_memories_definition() -> Tool:
    return Tool(
        name="list_memories",
        description=(
            "List all saved memory keys, optionally filtered by category. "
            "Use this to see what you've remembered."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Filter by category (optional)",
                },
            },
        },
    )


async def _list_memories_handle(arguments: dict) -> list[TextContent]:
    request = {}
    if arguments.get("category"):
        request["category"] = arguments["category"]
    return await ipc_service_request("list_memories", request)


register(
    "save_memory",
    ToolEntry(definition=_save_memory_definition, handler=_save_memory_handle),
)
register(
    "recall_memories",
    ToolEntry(definition=_recall_memories_definition, handler=_recall_memories_handle),
)
register(
    "forget_memory",
    ToolEntry(definition=_forget_memory_definition, handler=_forget_memory_handle),
)
register(
    "list_memories",
    ToolEntry(definition=_list_memories_definition, handler=_list_memories_handle),
)
