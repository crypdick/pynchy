"""Tests for bounded, concurrent MCP readiness at agent launch."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_settings

import pynchy.host.container_manager.mcp.manager as mcp_manager
from pynchy.config.mcp import McpServerConfig
from pynchy.config.models import ProfileConfig, WorkspaceConfig
from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp.manager import McpManager
from pynchy.host.container_manager.mcp.proxy import McpProxy


async def _synced_manager(tmp_path, monkeypatch: pytest.MonkeyPatch) -> McpManager:
    """Build manager state through its public configuration and sync APIs."""
    settings = make_settings(
        data_dir=tmp_path,
        profiles={"test": ProfileConfig(tools=["healthy", "broken"])},
        workspaces={"workspace": WorkspaceConfig(profiles=["test"])},
    )
    manager = McpManager(
        settings,
        MagicMock(spec=LiteLLMGateway),
        plugin_mcp_servers={
            "healthy": McpServerConfig(type="docker", image="healthy-image", port=8000),
            "broken": McpServerConfig(type="docker", image="broken-image", port=8001),
        },
    )
    monkeypatch.setattr(McpProxy, "start", AsyncMock(return_value=12345))
    monkeypatch.setattr(mcp_manager, "sync_mcp_endpoints", AsyncMock())
    monkeypatch.setattr(mcp_manager, "sync_teams", AsyncMock())

    def discard_background_task(coro, **_kwargs):
        coro.close()
        return MagicMock()

    monkeypatch.setattr(mcp_manager, "create_background_task", discard_background_task)

    await manager.sync()

    return manager


class TestWorkspaceMcpStartup:
    @pytest.mark.asyncio
    async def test_starts_instances_concurrently_and_preserves_healthy_tools(
        self, tmp_path, monkeypatch
    ):
        manager = await _synced_manager(tmp_path, monkeypatch)
        both_started = asyncio.Event()
        started: set[str] = set()

        async def start(instance) -> None:
            started.add(instance.instance_id)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.1)
            if instance.instance_id == "broken":
                raise TimeoutError("test timeout")

        monkeypatch.setattr(mcp_manager, "ensure_docker_running", start)

        result = await manager.ensure_workspace_running("workspace")

        assert result.ready_instance_ids == ("healthy",)
        assert len(result.failures) == 1
        assert result.failures[0].server_name == "broken"
        assert result.failures[0].reason == "start timed out"

    @pytest.mark.asyncio
    async def test_failed_instance_uses_retry_cooldown_without_repeating_notice(
        self, tmp_path, monkeypatch
    ):
        manager = await _synced_manager(tmp_path, monkeypatch)
        attempts = 0

        async def start(instance) -> None:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0)
            if instance.instance_id == "broken":
                raise RuntimeError("test failure")

        monkeypatch.setattr(mcp_manager, "ensure_docker_running", start)

        first = await manager.ensure_workspace_running("workspace")
        second = await manager.ensure_workspace_running("workspace")

        assert attempts == 3
        assert len(first.failures) == 1
        assert second.failures == ()
        assert second.ready_instance_ids == ("healthy",)
