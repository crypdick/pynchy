"""Public behavior of the agent-runner todo tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src"),
)

from agent_runner.agent_tools import AgentToolRuntime, call_tool, use_agent_tool_runtime


def _runtime(ipc_dir: Path) -> AgentToolRuntime:
    return AgentToolRuntime(
        chat_jid="slack:group",
        group_folder="group",
        is_admin=False,
        is_scheduled_task=False,
        ipc_dir=ipc_dir,
    )


@pytest.mark.asyncio
async def test_list_todos_reports_an_empty_todo_file(tmp_path: Path) -> None:
    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("list_todos", {})

    assert result[0].text == "No todo items."


@pytest.mark.asyncio
async def test_list_todos_tolerates_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "todos.json").write_text("not json", encoding="utf-8")

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("list_todos", {})

    assert result[0].text == "No todo items."


@pytest.mark.asyncio
async def test_complete_todo_updates_the_selected_item_atomically(tmp_path: Path) -> None:
    todos = [
        {"id": "first", "content": "first item", "done": False},
        {"id": "second", "content": "second item", "done": False},
    ]
    (tmp_path / "todos.json").write_text(json.dumps(todos), encoding="utf-8")

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("complete_todo", {"todo_id": "second"})

    assert result[0].text == "Todo second marked as done."
    assert json.loads((tmp_path / "todos.json").read_text(encoding="utf-8")) == [
        todos[0],
        {**todos[1], "done": True},
    ]
