"""Tests for the desktop screenshot MCP tool registration."""

from __future__ import annotations

import agent_runner.agent_tools._server  # noqa: F401  # allow: private-test-imports -- registration happens through module import side effects.
from agent_runner.agent_tools._registry import (
    all_tools,
)  # allow: private-test-imports -- public MCP roster is exposed through this internal registry.


def test_take_screenshot_tool_is_advertised() -> None:
    tools = {tool.name: tool for tool in all_tools()}

    assert "take_screenshot" in tools
    schema = tools["take_screenshot"].inputSchema
    assert schema["properties"]["mode"]["enum"] == ["full", "selection", "window"]
