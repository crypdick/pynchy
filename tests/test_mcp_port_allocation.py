"""Tests for per-instance port allocation and arg placeholder expansion."""

from __future__ import annotations

import subprocess  # noqa: S404, RUF100 - test fixtures construct completed Docker command results.
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_settings

from pynchy.config.mcp import McpServerConfig
from pynchy.config.models import ProfileConfig, WorkspaceConfig
from pynchy.config.settings import validate_settings_mapping
from pynchy.host.container_manager.docker import HealthCheckRequest
from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp.lifecycle import (
    build_env_args,
    ensure_docker_running,
    expand_arg_placeholders,
)
from pynchy.host.container_manager.mcp.manager import McpManager
from pynchy.host.container_manager.mcp.resolution import (
    McpInstance,
    merged_mcp_servers,
    resolve_all_instances,
)

ALL_INTERFACE_BIND_HOST = "0.0.0.0"  # noqa: S104, RUF100 - test fixture for pass-through MCP args that intentionally contain bind-all data.

# ---------------------------------------------------------------------------
# expand_arg_placeholders
# ---------------------------------------------------------------------------


class TestExpandArgPlaceholders:
    def test_basic_substitution(self):
        args = ["--port", "{port}", "--host", ALL_INTERFACE_BIND_HOST]
        result = expand_arg_placeholders(args, {"port": "9100"})
        assert result == ["--port", "9100", "--host", ALL_INTERFACE_BIND_HOST]

    def test_multiple_placeholders(self):
        args = ["--dir", "data/{workspace}/profiles", "--port", "{port}"]
        result = expand_arg_placeholders(args, {"workspace": "research", "port": "9101"})
        assert result == ["--dir", "data/research/profiles", "--port", "9101"]

    def test_no_op_passthrough(self):
        args = ["--headless", "--host", ALL_INTERFACE_BIND_HOST]
        result = expand_arg_placeholders(args, {"port": "9100"})
        assert result == ["--headless", "--host", ALL_INTERFACE_BIND_HOST]

    def test_missing_key_left_as_is(self):
        args = ["--dir", "{unknown}"]
        result = expand_arg_placeholders(args, {"port": "9100"})
        assert result == ["--dir", "{unknown}"]

    def test_empty_args(self):
        assert expand_arg_placeholders([], {"port": "9100"}) == []

    def test_empty_placeholders(self):
        args = ["--port", "{port}"]
        assert expand_arg_placeholders(args, {}) == ["--port", "{port}"]


class TestMcpServerConfig:
    def test_mcp_server_uses_short_startup_timeout_by_default(self):
        cfg = McpServerConfig(type="docker", image="img", port=8000)

        assert cfg.startup_timeout_seconds == pytest.approx(5.0)

    def test_mcp_server_rejects_non_positive_startup_timeout(self):
        with pytest.raises(ValueError, match="startup_timeout_seconds"):
            McpServerConfig(type="docker", image="img", port=8000, startup_timeout_seconds=0)

    def test_build_env_args_includes_static_env(self):
        cfg = McpServerConfig(
            type="docker",
            image="img",
            port=8000,
            env={"STATIC": "value"},
        )

        args = build_env_args(cfg)

        assert "-e" in args
        assert "STATIC=value" in args


class TestDockerLifecycleHelpers:
    def _stub_docker_lifecycle(self, monkeypatch) -> tuple[AsyncMock, AsyncMock]:
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.is_container_running",
            AsyncMock(return_value=False),
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle._ensure_mcp_image",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.ensure_network",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.remove_container",
            AsyncMock(),
        )
        run_docker_mock = AsyncMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.run_docker",
            run_docker_mock,
        )
        wait_healthy_mock = AsyncMock()
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.wait_healthy",
            wait_healthy_mock,
        )
        return run_docker_mock, wait_healthy_mock

    def _make_instance(
        self,
        *,
        port: int | None = 9100,
        extra_ports: list[int] | None = None,
        volumes: list[str] | None = None,
        args: list[str] | None = None,
        kwargs: dict[str, str] | None = None,
    ) -> McpInstance:
        cfg = McpServerConfig(
            type="docker",
            image="img",
            port=8000,
            extra_ports=extra_ports or [],
            args=args or [],
            volumes=volumes or [],
        )
        return McpInstance(
            server_name="browser",
            server_config=cfg,
            kwargs=kwargs or {},
            instance_id="browser",
            container_name="pynchy-mcp-browser",
            port=port,
        )

    @pytest.mark.asyncio
    async def test_ensure_docker_running_builds_expanded_docker_run_and_localhost_health_check(
        self,
        tmp_path,
        monkeypatch,
    ):
        instance = self._make_instance(
            port=9100,
            extra_ports=[9222, 9333],
            volumes=["groups/{workspace}:/workspace", "mcp-cache:/cache"],
            args=["--workspace-dir", "{workspace}", "--port", "{port}"],
            kwargs={"workspace": "research"},
        )
        settings = make_settings(project_root=tmp_path)
        monkeypatch.setattr("pynchy.config.get_settings", lambda: settings)

        run_docker_mock, wait_healthy_mock = self._stub_docker_lifecycle(monkeypatch)

        await ensure_docker_running(instance)

        assert run_docker_mock.await_count == 1
        assert run_docker_mock.await_args.args == (
            "run",
            "-d",
            "--name",
            "pynchy-mcp-browser",
            "--network",
            "pynchy-litellm-net",
            "--restart",
            "unless-stopped",
            "-p",
            "9100:8000",
            "-p",
            "9222:9222",
            "-p",
            "9333:9333",
            "-v",
            f"{tmp_path / 'groups' / 'research'}:/workspace",
            "-v",
            "mcp-cache:/cache",
            "img",
            "--workspace-dir",
            "research",
            "--port",
            "9100",
            "--workspace",
            "research",
        )
        assert wait_healthy_mock.await_args.args == (
            HealthCheckRequest(
                container_name="pynchy-mcp-browser",
                url="http://localhost:9100",
                any_non_5xx=True,
                health_timeout_seconds=5.0,
            ),
        )

    @pytest.mark.asyncio
    async def test_ensure_docker_running_omits_primary_publish_and_falls_back_to_container_url(
        self,
        monkeypatch,
    ):
        instance = self._make_instance(port=None, extra_ports=[9222])

        run_docker_mock, wait_healthy_mock = self._stub_docker_lifecycle(monkeypatch)

        await ensure_docker_running(instance)

        assert run_docker_mock.await_args.args == (
            "run",
            "-d",
            "--name",
            "pynchy-mcp-browser",
            "--network",
            "pynchy-litellm-net",
            "--restart",
            "unless-stopped",
            "-p",
            "9222:9222",
            "img",
        )
        assert wait_healthy_mock.await_args.args == (
            HealthCheckRequest(
                container_name="pynchy-mcp-browser",
                url="http://pynchy-mcp-browser:8000",
                any_non_5xx=True,
                health_timeout_seconds=5.0,
            ),
        )

    @pytest.mark.asyncio
    async def test_ensure_docker_running_captures_logs_before_removing_unhealthy_container(
        self,
        monkeypatch,
    ):
        instance = self._make_instance()
        run_docker_mock, wait_healthy_mock = self._stub_docker_lifecycle(monkeypatch)
        wait_healthy_mock.side_effect = TimeoutError("not ready")
        stop_container_mock = AsyncMock()
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.stop_container",
            stop_container_mock,
        )

        with pytest.raises(TimeoutError, match="not ready"):
            await ensure_docker_running(instance)

        assert run_docker_mock.await_args_list[-1].args == (
            "logs",
            "--tail",
            "50",
            "pynchy-mcp-browser",
        )
        stop_container_mock.assert_awaited_once_with("pynchy-mcp-browser", stop_timeout_seconds=1)


# ---------------------------------------------------------------------------
# merged_mcp_servers
# ---------------------------------------------------------------------------


class TestMergedMcpServers:
    def test_config_base_override_preserves_plugin_dockerfile_when_omitted(self):
        plugin_servers = {
            "gdrive": McpServerConfig(
                type="docker",
                image="pynchy-mcp-gdrive:latest",
                dockerfile="src/pynchy/agent/mcp/gdrive.Dockerfile",
                port=3100,
                transport="streamable_http",
                env={"GDRIVE_OAUTH_PATH": "/home/chrome/gcp-oauth.keys.json"},
            )
        }
        settings = validate_settings_mapping(
            {
                "tools": {
                    "gdrive": {
                        "type": "mcp",
                        "mcp": {
                            "runtime": "docker",
                            "image": "pynchy-mcp-gdrive:latest",
                            "port": 3000,
                            "transport": "streamable_http",
                            "env": {"GDRIVE_CREDENTIALS_PATH": "/gdrive-server/credentials.json"},
                            "volumes": [
                                "mcp-gdrive:/gdrive-server",
                                "data/gcp-oauth.keys.json:/app/gcp-oauth.keys.json:ro",
                            ],
                        },
                    }
                }
            }
        )

        merged = merged_mcp_servers(settings, plugin_servers)

        assert merged["gdrive"].dockerfile == "src/pynchy/agent/mcp/gdrive.Dockerfile"
        assert merged["gdrive"].port == 3000
        assert merged["gdrive"].env == {
            "GDRIVE_CREDENTIALS_PATH": "/gdrive-server/credentials.json"
        }
        assert merged["gdrive"].volumes == [
            "mcp-gdrive:/gdrive-server",
            "data/gcp-oauth.keys.json:/app/gcp-oauth.keys.json:ro",
        ]


# ---------------------------------------------------------------------------
# _resolve_all_instances port offset
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

        proxy_factory.assert_called_once_with(host=settings.gateway.host)

        parent_ids = manager.get_workspace_instance_ids("admin")
        child_ids = manager.get_workspace_instance_ids("admin__thread_discord-channel-thread")

        assert child_ids == parent_ids
        assert child_ids

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

    def test_linear_mcp_requires_explicit_tool_selection_with_api_key(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        settings = validate_settings_mapping(
            {
                "profiles": {"linear-access": {"tools": ["linear"]}},
                "workspaces": {
                    "alpha": {},
                    "beta": {"profiles": ["linear-access"]},
                },
                "tools": {
                    "linear": {
                        "type": "mcp",
                        "mcp": {
                            "runtime": "script",
                            "command": "uv",
                            "args": [
                                "run",
                                "python",
                                "-m",
                                "pynchy.plugins.integrations.linear",
                                "--port",
                                "{port}",
                                "--workspace",
                                "{workspace}",
                            ],
                            "port": 8474,
                            "transport": "streamable_http",
                            "inject_workspace": True,
                            "env_forward": {
                                "LINEAR_API_KEY": "LINEAR_API_KEY"  # pragma: allowlist secret
                            },
                        },
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
                env_forward={"LINEAR_API_KEY": "LINEAR_API_KEY"},  # pragma: allowlist secret
            )
        }

        state = resolve_all_instances(settings, all_servers)

        assert set(state.workspace_instances) == {"beta"}
        assert state.workspace_instances["beta"][0].startswith("linear_")
        assert len(state.instances) == 1
        assert next(iter(state.instances.values())).port == 8474
