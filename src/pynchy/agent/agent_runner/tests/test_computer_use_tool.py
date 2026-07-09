"""Tests for the computer_use MCP tool registration."""

from __future__ import annotations

import pytest

from agent_runner.agent_tools import list_tools


@pytest.mark.asyncio
async def test_computer_use_tool_is_advertised() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    assert "computer_use" in tools
    schema = tools["computer_use"].inputSchema
    assert schema["properties"]["action"]["enum"] == [
        "capture",
        "list_apps",
        "list_windows",
        "launch_app",
        "click",
        "double_click",
        "right_click",
        "type",
        "key",
        "scroll",
        "wait",
        "check_permissions",
    ]
