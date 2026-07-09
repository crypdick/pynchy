"""Tests for the computer_use MCP tool registration."""

from __future__ import annotations

import agent_runner.agent_tools._server  # noqa: F401  # allow: private-test-imports -- registration happens through module import side effects.
from agent_runner.agent_tools._registry import (
    all_tools,
)  # allow: private-test-imports -- public MCP roster is exposed through this internal registry.


def test_computer_use_tool_is_advertised() -> None:
    tools = {tool.name: tool for tool in all_tools()}

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
