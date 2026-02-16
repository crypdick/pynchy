"""Todo list MCP tools: list_todos, complete_todo.

Architecture note: The host writes to todos.json directly because the
Claude SDK does not expose an API to invoke MCP tools from outside the
agent's query loop.  The MCP server runs inside the container via stdio
and is only callable by the SDK during a query.  So the host edits the
JSON file, and these tools let the agent read/manage it.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp.types import CallToolResult, TextContent, Tool

from agent_runner.agent_tools._registry import ToolEntry, register

_TODOS_FILE = Path("/workspace/ipc/todos.json")


def _read_todos() -> list[dict]:
    if not _TODOS_FILE.exists():
        return []
    try:
        return json.loads(_TODOS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _write_todos(todos: list[dict]) -> None:
    _TODOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _TODOS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(todos, indent=2))
    tmp.rename(_TODOS_FILE)


# -- list_todos ----------------------------------------------------------------


def _list_todos_definition() -> Tool:
    return Tool(
        name="list_todos",
        description=(
            "List todo items for this group. The user can add items "
            "from outside the agent loop (via the 'todo' prefix), and "
            "you can manage them here."
        ),
        inputSchema={
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


async def _list_todos_handle(arguments: dict) -> list[TextContent]:
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


def _complete_todo_definition() -> Tool:
    return Tool(
        name="complete_todo",
        description="Mark a todo item as done by its ID.",
        inputSchema={
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


async def _complete_todo_handle(arguments: dict) -> list[TextContent] | CallToolResult:
    todo_id = arguments.get("todo_id", "")
    todos = _read_todos()

    for t in todos:
        if t.get("id") == todo_id:
            t["done"] = True
            _write_todos(todos)
            return [TextContent(type="text", text=f"Todo {todo_id} marked as done.")]

    return CallToolResult(
        content=[TextContent(type="text", text=f"Todo {todo_id} not found.")],
        isError=True,
    )


# -- registration --------------------------------------------------------------

register(
    "list_todos",
    ToolEntry(definition=_list_todos_definition, handler=_list_todos_handle),
)
register(
    "complete_todo",
    ToolEntry(definition=_complete_todo_definition, handler=_complete_todo_handle),
)
