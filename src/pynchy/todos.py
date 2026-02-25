"""Host-side todo list helpers — read/write todos.json per group.

The todo list is a JSON file stored in the group's IPC directory
(``data/ipc/{group_folder}/todos.json``).  This path is mounted into
the container at ``/workspace/ipc/todos.json``, so the agent can
read/manage it via the ``list_todos`` and ``complete_todo`` MCP tools.

Architecture note: the Claude SDK does not expose an API to invoke MCP
tools from outside the agent's query loop, so the host writes to the
JSON file directly.  The MCP tools inside the container provide a
read/manage interface for the agent.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pynchy.config import get_settings
from pynchy.ipc._write import write_json_atomic


def _todos_path(group_folder: str) -> Path:
    return get_settings().data_dir / "ipc" / group_folder / "todos.json"


def _read_todos(group_folder: str) -> list[dict[str, Any]]:
    path = _todos_path(group_folder)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _write_todos(group_folder: str, todos: list[dict[str, Any]]) -> None:
    write_json_atomic(_todos_path(group_folder), todos, indent=2)


def add_todo(group_folder: str, content: str) -> dict[str, Any]:
    """Append a todo item and return the new entry."""
    todos = _read_todos(group_folder)
    entry: dict[str, Any] = {
        "id": uuid.uuid4().hex[:8],
        "content": content,
        "done": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    todos.append(entry)
    _write_todos(group_folder, todos)
    return entry


def get_todos(group_folder: str) -> list[dict[str, Any]]:
    """Return all todo items for a group."""
    return _read_todos(group_folder)
