"""Public lifecycle-boundary coverage for the managed MCP manager."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_settings

import pynchy.host.container_manager.mcp.manager as manager_module
from pynchy.config.api import McpTool, McpToolConfig, ProfileConfig, WorkspaceConfig
from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp.manager import McpManager, configure_mcp_manager_runtime
from pynchy.host.container_manager.mcp.proxy import McpProxy
from pynchy.host.container_manager.mcp.resolution import (
    McpInstance,
    configure_mcp_resolution_runtime,
)
from pynchy.plugins.api import McpServerConfig


async def _synced_manager(tmp_path, monkeypatch, servers, workspace_tools):
    settings = make_settings(
        data_dir=tmp_path,
        tools={
            name: McpTool(
                type="mcp",
                mcp=McpToolConfig.model_validate(
                    {**config.model_dump(exclude={"type"}), "runtime": config.type}
                ),
            )
            for name, config in servers.items()
        },
        profiles={"test": ProfileConfig(tools=list(workspace_tools))},
        workspaces={"workspace": WorkspaceConfig(profiles=["test"])},
    )
    manager = McpManager(
        settings,
        MagicMock(spec=LiteLLMGateway),
        plugin_mcp_servers=servers,
    )
    monkeypatch.setattr(manager_module, "reap_stale_processes", lambda _path: 0)
    monkeypatch.setattr(McpProxy, "start", AsyncMock(return_value=12345))
    monkeypatch.setattr(manager_module, "sync_mcp_endpoints", AsyncMock())
    monkeypatch.setattr(manager_module, "sync_teams", AsyncMock())
    monkeypatch.setattr(manager_module, "load_teams_cache", lambda _path: {})
    monkeypatch.setattr(manager_module, "save_teams_cache", lambda *_args: None)
    monkeypatch.setattr(
        manager_module,
        "create_background_task",
        lambda coroutine, **_kwargs: coroutine.close() or MagicMock(),
    )
    await manager.sync()
    return manager


def _manager(tmp_path) -> McpManager:
    return McpManager(make_settings(data_dir=tmp_path), MagicMock(spec=LiteLLMGateway))


@pytest.mark.asyncio
async def test_sync_stops_after_resolution_finds_no_workspace_instances(tmp_path, monkeypatch):
    sync_endpoints = AsyncMock()
    monkeypatch.setattr(manager_module, "sync_mcp_endpoints", sync_endpoints)
    await _synced_manager(
        tmp_path,
        monkeypatch,
        {"browser": McpServerConfig(type="url", url="https://mcp.test/mcp")},
        (),
    )

    sync_endpoints.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_backend_lease_releases_nested_requests(tmp_path, monkeypatch):
    manager = await _synced_manager(
        tmp_path,
        monkeypatch,
        {"browser": McpServerConfig(type="script", command="run", port=9000)},
        ("browser",),
    )
    start = AsyncMock()
    monkeypatch.setattr(manager_module, "ensure_script_running", start)

    async with manager.proxy_backend_lease("browser"), manager.proxy_backend_lease("browser"):
        pass

    assert start.await_count == 2


@pytest.mark.asyncio
async def test_canary_rejects_docker_server_without_host_port(tmp_path, monkeypatch):
    manager = McpManager(
        make_settings(data_dir=tmp_path),
        MagicMock(spec=LiteLLMGateway),
        plugin_mcp_servers={"browser": McpServerConfig(type="docker", image="browser", port=9000)},
    )
    instance = McpInstance(
        server_name="browser",
        server_config=McpServerConfig(type="docker", image="browser", port=9000),
        kwargs={},
        instance_id="browser",
        container_name="browser",
        project_root=Path("/project"),
        port=None,
    )

    @dataclass(frozen=True)
    class _ResolvedState:
        instances: dict[str, McpInstance]
        workspace_instances: dict[str, list[str]]

    monkeypatch.setattr(
        manager_module,
        "resolve_all_instances",
        lambda _settings, _servers: _ResolvedState(
            {"browser": instance}, {"workspace": ["browser"]}
        ),
    )
    monkeypatch.setattr(manager_module, "reap_stale_processes", lambda _path: 0)
    monkeypatch.setattr(manager_module, "sync_mcp_endpoints", AsyncMock())
    monkeypatch.setattr(manager_module, "sync_teams", AsyncMock())
    monkeypatch.setattr(manager_module, "load_teams_cache", lambda _path: {})
    monkeypatch.setattr(manager_module, "save_teams_cache", lambda *_args: None)
    monkeypatch.setattr(
        manager_module,
        "create_background_task",
        lambda coroutine, **_kwargs: coroutine.close() or MagicMock(),
    )
    await manager.sync()
    start = AsyncMock()
    monkeypatch.setattr(manager_module, "ensure_docker_running", start)

    with pytest.raises(RuntimeError, match="has no host port"):
        await manager.get_canary_server_endpoint("browser")

    start.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_idle_ignores_url_exited_process_and_stopped_container(tmp_path, monkeypatch):
    servers = {
        "url": McpServerConfig(type="url", url="https://mcp.test/mcp"),
        "script": McpServerConfig(type="script", command="run", port=9000, idle_timeout=1),
        "docker": McpServerConfig(type="docker", image="browser", port=9001, idle_timeout=1),
    }
    manager = await _synced_manager(tmp_path, monkeypatch, servers, tuple(servers))
    process = MagicMock(spec=["poll"])
    process.poll.return_value = 0
    monkeypatch.setattr(
        manager_module,
        "ensure_script_running",
        AsyncMock(side_effect=lambda instance: setattr(instance, "process", process)),
    )
    terminate = MagicMock()
    stop = AsyncMock()
    monkeypatch.setattr(manager_module, "terminate_process", terminate)
    monkeypatch.setattr(manager_module, "is_container_running", AsyncMock(return_value=False))
    monkeypatch.setattr(manager_module, "stop_container", stop)
    monkeypatch.setattr(manager_module, "time", MagicMock(monotonic=lambda: 2.0))

    await manager.ensure_running("script")
    await manager.stop_idle()

    terminate.assert_not_called()
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_direct_server_configs_omits_routes_before_proxy_starts(tmp_path, monkeypatch):
    manager = await _synced_manager(
        tmp_path,
        monkeypatch,
        {"browser": McpServerConfig(type="url", url="https://mcp.test/mcp")},
        ("browser",),
    )
    configure_mcp_manager_runtime(
        static_workspace_folder=lambda folder: folder,
        load_resolved_workspace_config=lambda _folder, _settings: None,
    )

    configs = manager.get_direct_server_configs("workspace", invocation_ts=42.0)

    assert configs == []


def test_routed_workspace_without_resolved_policy_has_no_instances(tmp_path, monkeypatch):
    configure_mcp_resolution_runtime(
        apply_tool_access=lambda tools, resolved: (resolved, object()),
        tool_process_environment=lambda _tool: {},
    )
    settings = make_settings(
        data_dir=tmp_path,
        tools={
            "browser": McpTool(
                type="mcp",
                mcp=McpToolConfig(runtime="script", command="run", port=9000),
            )
        },
        profiles={"test": ProfileConfig(tools=["browser"])},
        workspaces={"parent": WorkspaceConfig(profiles=["test"])},
    )
    manager = McpManager(
        settings,
        MagicMock(spec=LiteLLMGateway),
        plugin_mcp_servers={"browser": McpServerConfig(type="script", command="run", port=9000)},
    )

    manager_module.configure_mcp_manager_runtime(
        static_workspace_folder=lambda _folder: "parent",
        load_resolved_workspace_config=lambda _folder, _settings: None,
    )
    assert manager.get_workspace_instance_ids("thread") == []
