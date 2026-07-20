"""Tests for the native Matrix communications tool registrations."""

from __future__ import annotations

import pytest

from agent_runner.agent_tools import list_tools


@pytest.mark.asyncio
async def test_matrix_gateway_tools_are_advertised() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    assert {name for name in tools if name.startswith("matrix_")} == {
        "matrix_route_read",
        "matrix_route_send",
    }
    assert "room_id" not in tools["matrix_route_read"].inputSchema["properties"]
    assert "room_id" not in tools["matrix_route_send"].inputSchema["properties"]
    assert tools["matrix_route_send"].inputSchema["required"] == ["body"]
    assert "requires human approval" in tools["matrix_route_send"].description
