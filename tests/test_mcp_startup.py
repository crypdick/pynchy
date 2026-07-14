"""Tests for bounded, concurrent MCP readiness at agent launch."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from conftest import make_settings

from pynchy.config.mcp import McpServerConfig
from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp.manager import McpManager
from pynchy.host.container_manager.mcp.resolution import McpInstance


def _instance(name: str) -> McpInstance:
    return McpInstance(
        server_name=name,
        server_config=McpServerConfig(type="docker", image="test-image", port=8000),
        kwargs={},
        instance_id=name,
        container_name=f"pynchy-mcp-{name}",
        port=8000,
    )


def _manager() -> McpManager:
    manager = McpManager(make_settings(), MagicMock(spec=LiteLLMGateway))
    manager._workspace_instances = {"workspace": ["healthy", "broken"]}
    manager._instances = {"healthy": _instance("healthy"), "broken": _instance("broken")}
    return manager


class TestWorkspaceMcpStartup:
    @pytest.mark.asyncio
    async def test_starts_instances_concurrently_and_preserves_healthy_tools(self, monkeypatch):
        manager = _manager()
        both_started = asyncio.Event()
        started: set[str] = set()

        async def start(instance_id: str) -> None:
            started.add(instance_id)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.1)
            if instance_id == "broken":
                raise TimeoutError("test timeout")

        monkeypatch.setattr(manager, "_ensure_running_unlocked", start)

        result = await manager.ensure_workspace_running("workspace")

        assert result.ready_instance_ids == ("healthy",)
        assert len(result.failures) == 1
        assert result.failures[0].server_name == "broken"
        assert result.failures[0].reason == "start timed out"

    @pytest.mark.asyncio
    async def test_failed_instance_uses_retry_cooldown_without_repeating_notice(self, monkeypatch):
        manager = _manager()
        attempts = 0

        async def start(instance_id: str) -> None:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0)
            if instance_id == "broken":
                raise RuntimeError("test failure")

        monkeypatch.setattr(manager, "_ensure_running_unlocked", start)

        first = await manager.ensure_workspace_running("workspace")
        second = await manager.ensure_workspace_running("workspace")

        assert attempts == 3
        assert len(first.failures) == 1
        assert second.failures == ()
        assert second.ready_instance_ids == ("healthy",)
