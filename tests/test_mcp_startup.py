"""Tests for bounded, concurrent MCP readiness at agent launch."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_settings

import pynchy.host.container_manager.mcp.manager as mcp_manager
from pynchy.config.api import McpTool, McpToolConfig, ProfileConfig, WorkspaceConfig
from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp.manager import McpManager
from pynchy.host.container_manager.mcp.proxy import McpBackendUnavailableError, McpProxy
from pynchy.plugins.api import McpServerConfig


async def _synced_manager(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    server_names: tuple[str, ...] = ("healthy", "broken"),
    server_configs: dict[str, McpServerConfig] | None = None,
    background_task_factory=None,
) -> McpManager:
    """Build manager state through its public configuration and sync APIs."""
    configs = server_configs or {
        "healthy": McpServerConfig(type="docker", image="healthy-image", port=8000),
        "broken": McpServerConfig(type="docker", image="broken-image", port=8001),
    }
    selected_configs = {name: configs[name] for name in server_names}
    settings = make_settings(
        data_dir=tmp_path,
        tools={
            name: McpTool(
                type="mcp",
                mcp=McpToolConfig.model_validate(
                    {
                        **config.model_dump(exclude={"type"}),
                        "runtime": config.type,
                    }
                ),
            )
            for name, config in selected_configs.items()
        },
        profiles={"test": ProfileConfig(tools=list(server_names))},
        workspaces={"workspace": WorkspaceConfig(profiles=["test"])},
    )
    manager = McpManager(
        settings,
        MagicMock(spec=LiteLLMGateway),
        plugin_mcp_servers=selected_configs,
    )
    monkeypatch.setattr(McpProxy, "start", AsyncMock(return_value=12345))
    monkeypatch.setattr(mcp_manager, "sync_mcp_endpoints", AsyncMock())
    monkeypatch.setattr(mcp_manager, "sync_teams", AsyncMock())

    def discard_background_task(coro, **_kwargs):
        coro.close()
        return MagicMock()

    monkeypatch.setattr(
        mcp_manager,
        "create_background_task",
        background_task_factory or discard_background_task,
    )

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


class TestMcpManagerLifecycleContracts:
    @pytest.mark.asyncio
    async def test_ensure_running_records_slow_backend_readiness(self, tmp_path, monkeypatch):
        config = McpServerConfig(type="script", command="run", port=8001)
        manager = await _synced_manager(
            tmp_path, monkeypatch, server_names=("script",), server_configs={"script": config}
        )
        ensure = AsyncMock()
        clock = iter((0.0, 1.0))
        monkeypatch.setattr(mcp_manager, "ensure_process_running", ensure)
        monkeypatch.setattr(mcp_manager, "time", MagicMock(monotonic=lambda: next(clock)))

        await manager.ensure_running("script")

        ensure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_idle_skips_already_stopped_host_process(self, tmp_path, monkeypatch):
        config = McpServerConfig(type="script", command="run", port=8001, idle_timeout=1)
        manager = await _synced_manager(
            tmp_path, monkeypatch, server_names=("script",), server_configs={"script": config}
        )
        ensure = AsyncMock()
        terminate = MagicMock()
        clock = [0.0]
        monkeypatch.setattr(mcp_manager, "ensure_process_running", ensure)
        monkeypatch.setattr(mcp_manager, "terminate_process", terminate)
        monkeypatch.setattr(mcp_manager, "time", MagicMock(monotonic=lambda: clock[0]))

        await manager.ensure_running("script")
        clock[0] = 2.0
        await manager.stop_idle()

        terminate.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_reaps_prior_process_ownership_once(self, tmp_path, monkeypatch):
        reaper = MagicMock(return_value=2)
        monkeypatch.setattr(mcp_manager, "reap_stale_processes", reaper)
        manager = McpManager(make_settings(data_dir=tmp_path), MagicMock(spec=LiteLLMGateway))

        await manager.sync()
        await manager.sync()

        reaper.assert_called_once_with(tmp_path / "mcp-processes")

    @pytest.mark.asyncio
    async def test_sync_with_no_configured_servers_is_a_noop(self, tmp_path, monkeypatch):
        manager = McpManager(make_settings(data_dir=tmp_path), MagicMock(spec=LiteLLMGateway))
        sync_endpoints = AsyncMock()
        monkeypatch.setattr(mcp_manager, "sync_mcp_endpoints", sync_endpoints)

        await manager.sync()

        sync_endpoints.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ensure_running_dispatches_host_processes_and_skips_urls(
        self, tmp_path, monkeypatch
    ):
        configs = {
            "script": McpServerConfig(type="script", command="run", port=8001),
            "stdio": McpServerConfig(type="stdio", command="run", port=8002, transport="http"),
            "url": McpServerConfig(type="url", url="https://mcp.example"),
        }
        manager = await _synced_manager(
            tmp_path, monkeypatch, server_names=tuple(configs), server_configs=configs
        )
        process = AsyncMock()
        docker = AsyncMock()
        monkeypatch.setattr(mcp_manager, "ensure_process_running", process)
        monkeypatch.setattr(mcp_manager, "ensure_docker_running", docker)

        await manager.ensure_running("script")
        await manager.ensure_running("stdio")
        await manager.ensure_running("url")
        await manager.ensure_running("unknown")

        assert [call.args[0].server_name for call in process.await_args_list] == ["script", "stdio"]
        docker.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_proxy_lease_allows_urls_and_rejects_unknown_instances(
        self, tmp_path, monkeypatch
    ):
        config = McpServerConfig(type="url", url="https://mcp.example")
        manager = await _synced_manager(
            tmp_path,
            monkeypatch,
            server_names=("url",),
            server_configs={"url": config},
        )

        async with manager.proxy_backend_lease("url"):
            pass

        with pytest.raises(McpBackendUnavailableError):
            async with manager.proxy_backend_lease("unknown"):
                pytest.fail("unknown backend yielded a lease")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            (McpServerConfig(type="url", url="https://mcp.example"), "https://mcp.example"),
            (
                McpServerConfig(type="script", command="run", port=8001, transport="http"),
                "http://localhost:8001/mcp",
            ),
            (
                McpServerConfig(type="script", command="run", port=8002),
                "http://localhost:8002",
            ),
        ],
    )
    async def test_canary_endpoint_starts_one_unambiguous_server(
        self, tmp_path, monkeypatch, config, expected
    ):
        manager = await _synced_manager(
            tmp_path,
            monkeypatch,
            server_names=("canary",),
            server_configs={"canary": config},
        )
        ensure_running = AsyncMock()
        monkeypatch.setattr(manager, "ensure_running", ensure_running)

        endpoint = await manager.get_canary_server_endpoint("canary")

        assert endpoint == expected
        ensure_running.assert_awaited_once_with("canary")

    @pytest.mark.asyncio
    async def test_canary_endpoint_uses_managed_container_url(self, tmp_path, monkeypatch):
        config = McpServerConfig(type="docker", image="image", port=8003)
        manager = await _synced_manager(
            tmp_path,
            monkeypatch,
            server_names=("canary",),
            server_configs={"canary": config},
        )
        monkeypatch.setattr(manager, "ensure_running", AsyncMock())
        container_url = MagicMock(return_value="http://managed:8003")
        monkeypatch.setattr(mcp_manager, "managed_container_url", container_url)

        assert await manager.get_canary_server_endpoint("canary") == "http://managed:8003"
        container_url.assert_called_once()

    @pytest.mark.asyncio
    async def test_canary_endpoint_rejects_missing_or_ambiguous_servers(
        self, tmp_path, monkeypatch
    ):
        manager = McpManager(make_settings(data_dir=tmp_path), MagicMock(spec=LiteLLMGateway))
        with pytest.raises(RuntimeError, match="No configured MCP server"):
            await manager.get_canary_server_endpoint("missing")

        config = McpServerConfig(type="script", command="run", port=8001)
        settings = make_settings(
            data_dir=tmp_path,
            tools={
                "shared": McpTool(
                    type="mcp",
                    mcp=McpToolConfig(
                        runtime="script", command="run", port=8001, inject_workspace=True
                    ),
                )
            },
            profiles={
                "first": ProfileConfig(tools=["shared"]),
                "second": ProfileConfig(tools=["shared"]),
            },
            workspaces={
                "one": WorkspaceConfig(profiles=["first"]),
                "two": WorkspaceConfig(profiles=["second"]),
            },
        )
        ambiguous = McpManager(
            settings, MagicMock(spec=LiteLLMGateway), plugin_mcp_servers={"shared": config}
        )
        monkeypatch.setattr(McpProxy, "start", AsyncMock(return_value=12345))
        monkeypatch.setattr(mcp_manager, "sync_mcp_endpoints", AsyncMock())
        monkeypatch.setattr(mcp_manager, "sync_teams", AsyncMock())
        monkeypatch.setattr(
            mcp_manager, "create_background_task", lambda coro, **_kwargs: coro.close()
        )
        await ambiguous.sync()

        with pytest.raises(RuntimeError, match="must resolve to one instance"):
            await ambiguous.get_canary_server_endpoint("shared")

    @pytest.mark.asyncio
    async def test_stop_idle_reaps_live_host_process_and_running_container(
        self, tmp_path, monkeypatch
    ):
        configs = {
            "script": McpServerConfig(type="script", command="run", port=8001, idle_timeout=1),
            "docker": McpServerConfig(type="docker", image="image", port=8002, idle_timeout=1),
            "permanent": McpServerConfig(type="docker", image="image", port=8003, idle_timeout=0),
        }
        manager = await _synced_manager(
            tmp_path, monkeypatch, server_names=tuple(configs), server_configs=configs
        )
        live_process = MagicMock(spec=["poll"])
        live_process.poll.return_value = None

        async def start_script(instance) -> None:
            await asyncio.sleep(0)
            instance.process = live_process

        terminate = MagicMock()
        running = AsyncMock(return_value=True)
        stop = AsyncMock()
        clock = [0.0]
        monkeypatch.setattr(mcp_manager, "ensure_process_running", start_script)
        monkeypatch.setattr(mcp_manager, "terminate_process", terminate)
        monkeypatch.setattr(mcp_manager, "is_container_running", running)
        monkeypatch.setattr(mcp_manager, "stop_container", stop)
        monkeypatch.setattr(mcp_manager, "time", MagicMock(monotonic=lambda: clock[0]))
        await manager.ensure_running("script")
        clock[0] = 2.0

        await manager.stop_idle()

        assert terminate.call_args.args[0].server_name == "script"
        assert running.await_args.args[0].endswith("-docker")
        assert stop.await_args.args[0].endswith("-docker")

    @pytest.mark.asyncio
    async def test_stop_all_stops_managed_backends_and_cancels_background_tasks(
        self, tmp_path, monkeypatch
    ):
        configs = {
            "script": McpServerConfig(type="script", command="run", port=8001),
            "docker": McpServerConfig(type="docker", image="image", port=8002),
            "url": McpServerConfig(type="url", url="https://mcp.example"),
        }
        background_tasks: list[MagicMock] = []
        proxy_stop = AsyncMock()
        monkeypatch.setattr(McpProxy, "stop", proxy_stop)

        def register_background_task(coro, **_kwargs):
            coro.close()
            task = MagicMock()
            background_tasks.append(task)
            return task

        manager = await _synced_manager(
            tmp_path,
            monkeypatch,
            server_names=tuple(configs),
            server_configs=configs,
            background_task_factory=register_background_task,
        )
        terminate = MagicMock()
        stop = AsyncMock()
        monkeypatch.setattr(mcp_manager, "terminate_process", terminate)
        monkeypatch.setattr(mcp_manager, "stop_container", stop)

        await manager.stop_all()

        proxy_stop.assert_awaited_once()
        assert [task.cancel.call_count for task in background_tasks] == [1, 1]
        assert terminate.call_args.args[0].server_name == "script"
        assert stop.await_args.args[0].endswith("-docker")
