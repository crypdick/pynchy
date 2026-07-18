"""Todo list MCP tools: list_todos, complete_todo.

Architecture note: The host writes to todos.json directly because the
Claude SDK does not expose an API to invoke MCP tools from outside the
agent's query loop.  The MCP server runs inside the container via stdio
and is only callable by the SDK during a query.  So the host edits the
JSON file, and these tools let the agent read/manage it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from mcp.types import CallToolResult, TextContent

from . import _ipc
from ._registry import tool, tool_error

if TYPE_CHECKING:
    from pathlib import Path


def _todos_file() -> Path:
    """Return the todo file for the active agent-tool runtime."""
    return _ipc.get_agent_tool_runtime().ipc_dir / "todos.json"


def _read_todos() -> list[dict[str, Any]]:
    todos_file = _todos_file()
    if not todos_file.exists():
        return []
    try:
        return cast("list[dict[str, Any]]", json.loads(todos_file.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return []


def _write_todos(todos: list[dict[str, Any]]) -> None:
    todos_file = _todos_file()
    todos_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = todos_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(todos, indent=2), encoding="utf-8")
    tmp.rename(todos_file)


# -- list_todos ----------------------------------------------------------------


@tool(
    "list_todos",
    (
        "List todo items for this group. The user can add items "
        "from outside the agent loop (via the 'todo' prefix), and "
        "you can manage them here."
    ),
    {
        "type": "object",
        "properties": {
            "include_done": {
                "type": "boolean",
                "default": False,
                "description": "Whether to include completed items.",
            },
        },
    },
)
async def _list_todos_handle(  # noqa: RUF029, RUF100 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent]:
    todos = _read_todos()
    include_done = arguments.get("include_done", False)
    if not include_done:
        todos = [t for t in todos if not t.get("done")]

    if not todos:
        return [TextContent(type="text", text="No todo items.")]

    lines = []
    for t in todos:
        status = "done" if t.get("done") else "pending"
        lines.append(f"- [{t['id']}] ({status}) {t['content']}")

    return [TextContent(type="text", text=f"Todo items:\n{chr(10).join(lines)}")]


# -- complete_todo -------------------------------------------------------------


@tool(
    "complete_todo",
    "Mark a todo item as done by its ID.",
    {
        "type": "object",
        "properties": {
            "todo_id": {
                "type": "string",
                "description": "The ID of the todo item to complete.",
            },
        },
        "required": ["todo_id"],
    },
)
async def _complete_todo_handle(  # noqa: RUF029, RUF100 - async tool API.
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    todo_id = arguments.get("todo_id", "")
    todos = _read_todos()

    for t in todos:
        if t.get("id") == todo_id:
            t["done"] = True
            _write_todos(todos)
            return [TextContent(type="text", text=f"Todo {todo_id} marked as done.")]

    return tool_error(f"Todo {todo_id} not found.")
