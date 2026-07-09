"""Tests for the desktop screenshot MCP tool registration."""

from __future__ import annotations

import pytest

from agent_runner.agent_tools import list_tools


async def _tools_by_name():
    return {tool.name: tool for tool in await list_tools()}


@pytest.mark.asyncio
async def test_take_screenshot_tool_is_advertised() -> None:
    tools = await _tools_by_name()

    assert "take_screenshot" in tools
    schema = tools["take_screenshot"].inputSchema
    assert schema["properties"]["mode"]["enum"] == ["full", "selection", "window"]


@pytest.mark.asyncio
async def test_analyze_screenshot_tool_is_advertised() -> None:
    tools = await _tools_by_name()

    assert "analyze_screenshot" in tools
    schema = tools["analyze_screenshot"].inputSchema
    assert "image_path" in schema["properties"]
    assert "prompt" in schema["properties"]
