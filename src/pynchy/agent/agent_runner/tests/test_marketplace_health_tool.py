"""Tests for the marketplace health agent-tool contract."""

from __future__ import annotations

import pytest

from agent_runner.agent_tools import list_tools


@pytest.mark.asyncio
async def test_marketplace_health_tool_is_bounded_and_argument_free() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    tool = tools["marketplace_health_snapshot"]
    assert tool.inputSchema == {"type": "object", "properties": {}}
    assert "no buyer, listing, or email content" in tool.description
    assert "does not mutate" in tool.description
