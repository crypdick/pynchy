"""Public task-definition MCP contract behavior."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)

from agent_runner.agent_tools import AgentToolRuntime, call_tool, use_agent_tool_runtime


def _runtime(tmp_path: Path) -> AgentToolRuntime:
    return AgentToolRuntime(
        chat_jid="test@g.us",
        group_folder="test-group",
        is_admin=False,
        is_scheduled_task=False,
        ipc_dir=tmp_path,
    )


def _definition() -> dict[str, object]:
    return {
        "id": "task-1",
        "group": "test-group",
        "prompt": "repair task path",
        "schedule_type": "cron",
        "schedule_value": "0 9 * * *",
        "session_policy": "reset_before_run",
        "status": "paused",
        "memory_enabled": True,
    }


@pytest.mark.asyncio
@pytest.mark.action("task.read")
async def test_get_scheduled_task_returns_validated_definition(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_tasks.ipc_service_request",
        AsyncMock(return_value=[TextContent(type="text", text=json.dumps(_definition()))]),
    )

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("get_scheduled_task", {"task_id": "task-1"})

    assert result[1] == _definition()


@pytest.mark.asyncio
@pytest.mark.action("task.update")
async def test_update_scheduled_task_rejects_malformed_host_definition(
    monkeypatch, tmp_path: Path
) -> None:
    malformed = _definition()
    malformed["bound_group_folder"] = "private"
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_tasks.ipc_service_request",
        AsyncMock(return_value=[TextContent(type="text", text=json.dumps(malformed))]),
    )

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("update_scheduled_task", {"task_id": "task-1", "status": "paused"})

    assert result.isError is True
    assert "invalid fields" in result.content[0].text
