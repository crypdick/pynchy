"""Memory tools — save, recall, forget, and list memories via IPC service requests.

These tools provide persistent, searchable memory across sessions.
Memories are stored per-workspace with BM25-ranked full-text search.
"""

from agent_runner.agent_tools._registry import register_ipc_tool

register_ipc_tool(
    name="save_memory",
    description=(
        "Save a fact, preference, or note to persistent memory. "
        "Use a descriptive key (e.g., 'user-favorite-color', 'project-deadline'). "
        "If the key already exists, the content is updated."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": ("Unique identifier for this memory (e.g., 'user-favorite-color')"),
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

register_ipc_tool(
    name="recall_memories",
    description=(
        "Search memories by keyword. Returns the most relevant matches "
        "ranked by BM25 relevance scoring. Use this to recall previously "
        "saved facts, preferences, or context."
    ),
    input_schema={
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

register_ipc_tool(
    name="forget_memory",
    description="Delete a memory by key. Use this to remove outdated or incorrect information.",
    input_schema={
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

register_ipc_tool(
    name="list_memories",
    description=(
        "List all saved memory keys, optionally filtered by category. "
        "Use this to see what you've remembered."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Filter by category (optional)",
            },
        },
    },
)
