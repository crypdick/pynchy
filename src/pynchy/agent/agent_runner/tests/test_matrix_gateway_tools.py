"""Tests for the native Matrix communications tool registrations."""

from __future__ import annotations

import pytest

from agent_runner.agent_tools import list_tools


@pytest.mark.asyncio
async def test_matrix_gateway_tools_are_advertised() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    assert set(tools) >= {
        "matrix_list_chats",
        "matrix_list_messages",
        "matrix_send_message",
    }
    assert tools["matrix_send_message"].inputSchema["required"] == ["room_id", "body"]
    assert "requires approval" in tools["matrix_send_message"].description
