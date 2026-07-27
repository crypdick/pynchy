"""Tests for bounded, concurrent MCP readiness at agent launch."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_settings

import pynchy.host.container_manager.mcp.manager as mcp_manager
from pynchy.config.models import McpTool, McpToolConfig, ProfileConfig, WorkspaceConfig
from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp.manager import McpManager
from pynchy.host.container_manager.mcp.proxy import McpBackendUnavailableError, McpProxy
from pynchy.plugins.mcp_server import McpServerConfig


async def _synced_manager(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    server_names: tuple[str, ...] = ("healthy", "broken"),
) -> McpManager:
    """Build manager state through its public configuration and sync APIs."""
    server_configs = {
        "healthy": McpServerConfig(type="docker", image="healthy-image", port=8000),
        "broken": McpServerConfig(type="docker", image="broken-image", port=8001),
    }
    settings = make_settings(
        data_dir=tmp_path,
        tools={
            name: McpTool(
                type="mcp",
                mcp=McpToolConfig(
                    runtime="docker",
                    image=server_configs[name].image,
                    port=server_configs[name].port,
                ),
            )
            for name in server_names
        },
        profiles={"test": ProfileConfig(tools=list(server_names))},
        workspaces={"workspace": WorkspaceConfig(profiles=["test"])},
    )
    manager = McpManager(
        settings,
        MagicMock(spec=LiteLLMGateway),
        plugin_mcp_servers={name: server_configs[name] for name in server_names},
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
            await both_started.wait()
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

    @pytest.mark.asyncio
    async def test_second_request_refresh_prevents_false_idle_reap(self, tmp_path, monkeypatch):
        """Recent request traffic should reset the managed backend's idle clock."""
        manager = await _synced_manager(
            tmp_path,
            monkeypatch,
            server_names=("healthy",),
        )
        ensure = AsyncMock()
        is_running = AsyncMock(return_value=True)
        stop = AsyncMock()
        clock = [1000.0]
        monkeypatch.setattr(mcp_manager, "ensure_docker_running", ensure)
        monkeypatch.setattr(mcp_manager, "is_container_running", is_running)
        monkeypatch.setattr(mcp_manager, "stop_container", stop)
        monkeypatch.setattr(
            mcp_manager,
            "time",
            MagicMock(monotonic=MagicMock(side_effect=lambda: clock[0])),
        )

        async with manager.proxy_backend_lease("healthy"):
            pass
        clock[0] = 1601.0
        async with manager.proxy_backend_lease("healthy"):
            pass
        clock[0] = 1602.0
        await manager.stop_idle()

        assert ensure.await_count == 2
        is_running.assert_not_awaited()
        stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_request_ensure_recovers_after_concurrent_idle_reap(self, tmp_path, monkeypatch):
        """A request racing idle cleanup should ensure the backend after the reap."""
        manager = await _synced_manager(
            tmp_path,
            monkeypatch,
            server_names=("healthy",),
        )
        idle_check_started = asyncio.Event()
        release_idle_check = asyncio.Event()
        events: list[str] = []
        backend_running = True
        clock = [1000.0]

        monkeypatch.setattr(
            mcp_manager,
            "time",
            MagicMock(monotonic=MagicMock(side_effect=lambda: clock[0])),
        )

        async def is_running(_container_name: str) -> bool:
            events.append("idle-check")
            idle_check_started.set()
            await release_idle_check.wait()
            return backend_running

        async def stop(_container_name: str) -> None:
            nonlocal backend_running
            await asyncio.sleep(0)
            events.append("stop")
            backend_running = False

        async def ensure(_instance) -> None:
            nonlocal backend_running
            await asyncio.sleep(0)
            events.append("ensure")
            backend_running = True

        monkeypatch.setattr(mcp_manager, "is_container_running", is_running)
        monkeypatch.setattr(mcp_manager, "stop_container", stop)
        monkeypatch.setattr(mcp_manager, "ensure_docker_running", ensure)

        async with manager.proxy_backend_lease("healthy"):
            pass
        events.clear()
        clock[0] = 1601.0

        idle_task = asyncio.create_task(manager.stop_idle())
        await idle_check_started.wait()

        async def proxy_request() -> None:
            async with manager.proxy_backend_lease("healthy"):
                events.append("forward")

        request_task = asyncio.create_task(proxy_request())
        await asyncio.sleep(0)

        assert not request_task.done()
        release_idle_check.set()
        await asyncio.gather(idle_task, request_task)

        assert backend_running
        assert events == ["idle-check", "stop", "ensure", "forward"]

    @pytest.mark.asyncio
    async def test_proxy_lease_honors_startup_failure_cooldown(self, tmp_path, monkeypatch):
        """Repeated proxy traffic should not loop a failing backend startup."""
        manager = await _synced_manager(
            tmp_path,
            monkeypatch,
            server_names=("healthy",),
        )
        ensure = AsyncMock(side_effect=RuntimeError("health timeout"))
        clock = [1000.0]
        monkeypatch.setattr(mcp_manager, "ensure_docker_running", ensure)
        monkeypatch.setattr(
            mcp_manager,
            "time",
            MagicMock(monotonic=MagicMock(side_effect=lambda: clock[0])),
        )

        for _attempt in range(2):
            with pytest.raises(McpBackendUnavailableError):
                async with manager.proxy_backend_lease("healthy"):
                    pytest.fail("unavailable backend lease yielded")

        assert ensure.await_count == 1

        clock[0] = 1301.0
        with pytest.raises(McpBackendUnavailableError):
            async with manager.proxy_backend_lease("healthy"):
                pytest.fail("failed backend lease yielded")
        assert ensure.await_count == 2

    @pytest.mark.asyncio
    async def test_idle_reap_skips_long_in_flight_proxy_request(self, tmp_path, monkeypatch):
        """Idle cleanup should not stop a backend while its proxy lease is active."""
        manager = await _synced_manager(
            tmp_path,
            monkeypatch,
            server_names=("healthy",),
        )
        clock = [1000.0]
        is_running = AsyncMock(return_value=True)
        stop = AsyncMock()
        monkeypatch.setattr(mcp_manager, "ensure_docker_running", AsyncMock())
        monkeypatch.setattr(mcp_manager, "is_container_running", is_running)
        monkeypatch.setattr(mcp_manager, "stop_container", stop)
        monkeypatch.setattr(
            mcp_manager,
            "time",
            MagicMock(monotonic=MagicMock(side_effect=lambda: clock[0])),
        )

        async with manager.proxy_backend_lease("healthy"):
            clock[0] = 2000.0
            await manager.stop_idle()

        clock[0] = 2599.0
        await manager.stop_idle()

        is_running.assert_not_awaited()
        stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancelled_proxy_request_releases_lease(self, tmp_path, monkeypatch):
        """Cancellation should release activity accounting and refresh the idle clock."""
        manager = await _synced_manager(
            tmp_path,
            monkeypatch,
            server_names=("healthy",),
        )
        clock = [1000.0]
        entered = asyncio.Event()
        never = asyncio.Event()
        stop = AsyncMock()
        monkeypatch.setattr(mcp_manager, "ensure_docker_running", AsyncMock())
        monkeypatch.setattr(mcp_manager, "is_container_running", AsyncMock(return_value=True))
        monkeypatch.setattr(mcp_manager, "stop_container", stop)
        monkeypatch.setattr(
            mcp_manager,
            "time",
            MagicMock(monotonic=MagicMock(side_effect=lambda: clock[0])),
        )

        async def proxy_request() -> None:
            async with manager.proxy_backend_lease("healthy"):
                entered.set()
                await never.wait()

        request = asyncio.create_task(proxy_request())
        await entered.wait()
        clock[0] = 2000.0
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        clock[0] = 2601.0
        await manager.stop_idle()
        stop.assert_awaited_once()
