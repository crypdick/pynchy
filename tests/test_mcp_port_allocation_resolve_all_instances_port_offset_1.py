"""Tests for per-instance port allocation and arg placeholder expansion."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.config.api import ProfileConfig, WorkspaceConfig, validate_settings_mapping
from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp.manager import McpManager
from pynchy.host.container_manager.mcp.resolution import (
    merged_mcp_servers,
    resolve_all_instances,
)
from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspaceRestriction,
    clear_runtime_workspace_restrictions,
    register_runtime_workspace_restriction,
)
from pynchy.plugins.api import McpServerConfig

ALL_INTERFACE_BIND_HOST = "0.0.0.0"  # noqa: S104 - test fixture for pass-through MCP args that intentionally contain bind-all data.

# ---------------------------------------------------------------------------
# expand_arg_placeholders
# ---------------------------------------------------------------------------


class TestResolveAllInstancesPortOffset:
    """Port allocation in _resolve_all_instances.

    inject_workspace=True creates separate instances per workspace (each
    gets workspace=<folder> in kwargs → unique instance ID).  Without it,
    two workspaces sharing the same server share one instance.
    """

    def _settings(self, workspaces: dict, tool_mcp_configs: dict):
        ws_configs = {}
        profiles = {}
        for name, servers in workspaces.items():
            profile_name = f"{name}-mcp"
            profiles[profile_name] = ProfileConfig(tools=servers)
            ws_configs[name] = WorkspaceConfig(profiles=[profile_name])

        return validate_settings_mapping(
            {
                "profiles": {
                    name: profile.model_dump(exclude_defaults=True)
                    for name, profile in profiles.items()
                },
                "workspaces": {
                    name: workspace.model_dump(exclude_defaults=True)
                    for name, workspace in ws_configs.items()
                },
                "tools": {
                    name: {"type": "mcp", "mcp": spec} for name, spec in tool_mcp_configs.items()
                },
            }
        )

    def _resolve_instances(self, workspaces: dict, tool_mcp_configs: dict):
        settings = self._settings(workspaces, tool_mcp_configs)
        return resolve_all_instances(settings, merged_mcp_servers(settings, {}))

    def test_inject_workspace_two_workspaces_get_different_ports(self):
        state = self._resolve_instances(
            workspaces={
                "ws1": ["browser"],
                "ws2": ["browser"],
            },
            tool_mcp_configs={
                "browser": {
                    "runtime": "script",
                    "command": "npx",
                    "port": 9100,
                    "inject_workspace": True,
                },
            },
        )
        ports = sorted(inst.port for inst in state.instances.values())
        assert ports == [9100, 9101]

    def test_single_workspace_gets_base_port(self):
        state = self._resolve_instances(
            workspaces={"ws1": ["browser"]},
            tool_mcp_configs={
                "browser": {
                    "runtime": "script",
                    "command": "npx",
                    "port": 9100,
                },
            },
        )
        inst = next(iter(state.instances.values()))
        assert inst.port == 9100

    def test_distinct_servers_with_the_same_base_port_do_not_collide(self):
        state = self._resolve_instances(
            workspaces={
                "public": ["linear-public"],
                "synapse": ["linear-synapse"],
            },
            tool_mcp_configs={
                "linear-public": {
                    "runtime": "script",
                    "command": "uv",
                    "port": 8474,
                },
                "linear-synapse": {
                    "runtime": "script",
                    "command": "uv",
                    "port": 8474,
                },
            },
        )

        assert sorted(instance.port for instance in state.instances.values()) == [8474, 8475]

    def test_host_service_ports_are_not_assigned_to_mcp_processes(self):
        settings = validate_settings_mapping(
            {
                "server": {"port": 9100},
                "gateway": {"port": 9101, "mcp_proxy_port": 9102},
                "profiles": {"tools": {"tools": ["browser"]}},
                "workspaces": {"admin": {"profiles": ["tools"]}},
                "tools": {
                    "browser": {
                        "type": "mcp",
                        "mcp": {
                            "runtime": "script",
                            "command": "npx",
                            "port": 9100,
                        },
                    }
                },
            }
        )

        state = resolve_all_instances(settings, merged_mcp_servers(settings, {}))

        assert next(iter(state.instances.values())).port == 9103

    @pytest.mark.asyncio
    async def test_sync_makes_parent_workspace_instances_available_to_dynamic_threads(
        self, monkeypatch
    ):
        settings = self._settings(
            workspaces={"admin": ["browser"]},
            tool_mcp_configs={
                "browser": {
                    "runtime": "script",
                    "command": "npx",
                    "port": 9100,
                },
            },
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.workspace_config.get_settings",
            lambda: settings,
        )

        def close_background_task(coro, **_kwargs):
            coro.close()
            return MagicMock()

        proxy_start = AsyncMock(return_value=0)
        proxy = MagicMock()
        proxy.start = proxy_start
        proxy_factory = MagicMock(return_value=proxy)
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.McpProxy",
            proxy_factory,
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.sync_mcp_endpoints", AsyncMock()
        )
        monkeypatch.setattr("pynchy.host.container_manager.mcp.manager.sync_teams", AsyncMock())
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.load_teams_cache", lambda _: {}
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.save_teams_cache", lambda *_: None
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.create_background_task",
            close_background_task,
        )

        manager = McpManager(settings, MagicMock(spec=LiteLLMGateway))
        await manager.sync()

        proxy_factory.assert_called_once()
        proxy_options = proxy_factory.call_args.kwargs
        assert proxy_options["host"] == settings.gateway.host
        assert proxy_options["backend_lease"] == manager.proxy_backend_lease

        parent_ids = manager.get_workspace_instance_ids("admin")
        child_ids = manager.get_workspace_instance_ids("admin__thread_discord-channel-thread")

        assert child_ids == parent_ids
        assert child_ids
        authorize_instance = proxy_options["authorize_instance"]
        assert authorize_instance("admin__thread_discord-channel-thread", child_ids[0])
        assert not authorize_instance("unknown", child_ids[0])

    @pytest.mark.asyncio
    async def test_runtime_route_restriction_removes_parent_mcp_instances(self, monkeypatch):
        settings = self._settings(
            workspaces={"admin": ["browser"]},
            tool_mcp_configs={
                "browser": {
                    "runtime": "script",
                    "command": "npx",
                    "port": 9100,
                },
            },
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.workspace_config.get_settings",
            lambda: settings,
        )
        proxy = MagicMock()
        proxy.start = AsyncMock(return_value=0)
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.McpProxy",
            MagicMock(return_value=proxy),
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.sync_mcp_endpoints",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.sync_teams",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.load_teams_cache",
            lambda _: {},
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.save_teams_cache",
            lambda *_: None,
        )

        def close_background_task(coro, **_kwargs):
            coro.close()
            return MagicMock()

        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.manager.create_background_task",
            close_background_task,
        )
        manager = McpManager(settings, MagicMock(spec=LiteLLMGateway))
        await manager.sync()
        child = "admin__thread_discord-channel-restricted"
        register_runtime_workspace_restriction(
            child,
            RuntimeWorkspaceRestriction(parent_workspace="admin", tools=()),
        )

        try:
            instance_ids = manager.get_workspace_instance_ids(child)
        finally:
            clear_runtime_workspace_restrictions()

        assert instance_ids == []

    def test_inject_workspace_independent_port_counters_per_server(self):
        state = self._resolve_instances(
            workspaces={
                "ws1": ["browser", "notebook"],
                "ws2": ["browser", "notebook"],
            },
            tool_mcp_configs={
                "browser": {
                    "runtime": "script",
                    "command": "npx",
                    "port": 9100,
                    "inject_workspace": True,
                },
                "notebook": {
                    "runtime": "script",
                    "command": "uv",
                    "port": 8888,
                    "inject_workspace": True,
                },
            },
        )
        browser_ports = sorted(
            inst.port for inst in state.instances.values() if inst.server_name == "browser"
        )
        notebook_ports = sorted(
            inst.port for inst in state.instances.values() if inst.server_name == "notebook"
        )
        assert browser_ports == [9100, 9101]
        assert notebook_ports == [8888, 8889]

    def test_shared_instance_no_duplicate_port(self):
        """Two workspaces with no per-workspace kwargs share one instance."""
        state = self._resolve_instances(
            workspaces={
                "ws1": ["search"],
                "ws2": ["search"],
            },
            tool_mcp_configs={
                "search": {
                    "runtime": "script",
                    "command": "node",
                    "port": 7000,
                },
            },
        )
        # Same instance ID (no kwargs → no hash), so only one instance
        assert len(state.instances) == 1
        inst = next(iter(state.instances.values()))
        assert inst.port == 7000

    def test_url_type_gets_none_port(self):
        state = self._resolve_instances(
            workspaces={"ws1": ["remote"]},
            tool_mcp_configs={
                "remote": {
                    "runtime": "url",
                    "url": "https://example.com/mcp",
                },
            },
        )
        inst = next(iter(state.instances.values()))
        assert inst.port is None

    def test_profile_tools_are_resolved_for_workspace(self):
        settings = validate_settings_mapping(
            {
                "profiles": {"dev": {"tools": ["linear"]}},
                "workspaces": {"code-improver": {"profiles": ["dev"]}},
                "tools": {
                    "linear": {
                        "type": "mcp",
                        "mcp": {
                            "runtime": "script",
                            "command": "uv",
                            "port": 8474,
                            "transport": "streamable_http",
                        },
                    }
                },
            }
        )

        state = resolve_all_instances(settings, merged_mcp_servers(settings, {}))

        assert list(state.instances) == ["linear"]
        assert state.workspace_instances == {"code-improver": ["linear"]}

    def test_profile_tools_are_resolved_for_semantic_workspace(self):
        settings = validate_settings_mapping(
            {
                "profiles": {
                    "base": {"tools": ["linear"]},
                    "pynchy-dev": {"includes": ["base"]},
                },
                "workspaces": {
                    "admin": {
                        "scopes": [
                            {
                                "workspace": "pynchy-dev",
                                "profiles": ["pynchy-dev"],
                            }
                        ]
                    }
                },
                "tools": {
                    "linear": {
                        "type": "mcp",
                        "mcp": {
                            "runtime": "script",
                            "command": "uv",
                            "port": 8474,
                            "transport": "streamable_http",
                            "inject_workspace": True,
                        },
                    }
                },
            }
        )

        state = resolve_all_instances(settings, merged_mcp_servers(settings, {}))

        assert list(state.workspace_instances) == ["pynchy-dev"]
        instance_id = state.workspace_instances["pynchy-dev"][0]
        instance = state.instances[instance_id]
        assert instance.server_name == "linear"
        assert instance.kwargs == {"workspace": "pynchy-dev"}
        assert instance.port == 8474

    def test_linear_mcp_requires_explicit_tool_selection_with_api_key(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "selected-value")
        monkeypatch.setenv("LINEAR_TEAM_KEY", "team-value")
        settings = validate_settings_mapping(
            {
                "profiles": {"linear-access": {"tools": ["linear"]}},
                "workspaces": {
                    "alpha": {},
                    "beta": {"profiles": ["linear-access"]},
                },
                "tools": {
                    "linear": {
                        "type": "linear",
                    }
                },
            }
        )
        all_servers = {
            "linear": McpServerConfig(
                type="script",
                command="uv",
                args=[
                    "run",
                    "python",
                    "-m",
                    "pynchy.plugins.integrations.linear",
                    "--port",
                    "{port}",
                    "--workspace",
                    "{workspace}",
                ],
                port=8474,
                transport="streamable_http",
                inject_workspace=True,
            )
        }

        state = resolve_all_instances(settings, all_servers)

        assert set(state.workspace_instances) == {"beta"}
        assert state.workspace_instances["beta"][0].startswith("linear_")
        assert len(state.instances) == 1
        instance = next(iter(state.instances.values()))
        assert instance.port == 8474
        assert instance.tool_environment == {
            "LINEAR_API_KEY": "selected-value",  # pragma: allowlist secret
            "LINEAR_TEAM_KEY": "team-value",  # pragma: allowlist secret
        }

    def test_missing_required_environment_removes_the_mcp_instance(self, monkeypatch):
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        settings = validate_settings_mapping(
            {
                "profiles": {"linear-access": {"tools": ["linear"]}},
                "workspaces": {"beta": {"profiles": ["linear-access"]}},
                "tools": {"linear": {"type": "linear"}},
            }
        )
        all_servers = {
            "linear": McpServerConfig(
                type="script",
                command="uv",
                port=8474,
                transport="streamable_http",
            )
        }

        state = resolve_all_instances(settings, all_servers)

        assert state.instances == {}
        assert state.workspace_instances == {}
