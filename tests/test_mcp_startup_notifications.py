"""Tests for channel-visible MCP startup failures."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import make_container_runtime_operations

from pynchy.host.container_manager.api import McpStartupFailure
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.mcp_notifications import notify_mcp_startup_failures


class _Deps:
    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.session_cleared: set[str] = set()
        self.workspaces: dict[str, object] = {}
        self.queue = GroupQueue(
            10,
            make_container_runtime_operations(),
        )
        self.plugin_manager = None
        self.broadcast_host_message = AsyncMock()

    async def get_available_groups(self) -> list[dict[str, Any]]:
        return []

    async def broadcast_agent_input(
        self, _chat_jid: str, _messages: list[dict[str, Any]], *, source: str = "user"
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_mcp_startup_failure_is_sent_as_host_message() -> None:
    deps = _Deps()

    await notify_mcp_startup_failures(
        deps.broadcast_host_message,
        "discord:channel-1",
        (
            McpStartupFailure(
                instance_id="gcal",
                server_name="gcal",
                reason="start timed out",
            ),
        ),
    )

    deps.broadcast_host_message.assert_awaited_once_with(
        "discord:channel-1",
        "⚠️ MCP tool unavailable: gcal (start timed out). Continuing without it; "
        "Pynchy will retry in 5 minutes.",
    )


@pytest.mark.asyncio
async def test_mcp_startup_notice_delivery_failure_does_not_block_agent() -> None:
    broadcast = AsyncMock(side_effect=RuntimeError("channel unavailable"))

    await notify_mcp_startup_failures(
        broadcast,
        "discord:channel-1",
        (McpStartupFailure(instance_id="gcal", server_name="gcal", reason="start timed out"),),
    )

    broadcast.assert_awaited_once()
