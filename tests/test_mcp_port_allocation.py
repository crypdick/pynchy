"""Tests for per-instance port allocation and arg placeholder expansion."""

from __future__ import annotations

from unittest.mock import MagicMock

from conftest import make_settings

from pynchy.config.mcp import McpServerConfig
from pynchy.config.models import ProfileConfig, WorkspaceConfig
from pynchy.config.settings import validate_settings_mapping
from pynchy.host.container_manager.gateway_litellm import LiteLLMGateway
from pynchy.host.container_manager.mcp.lifecycle import (
    _build_placeholders,  # allow: private-test-imports
    _docker_health_url,  # allow: private-test-imports
    _docker_publish_args,  # allow: private-test-imports
    _docker_volume_args,  # allow: private-test-imports
    build_env_args,
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


# ---------------------------------------------------------------------------
# _build_placeholders
# ---------------------------------------------------------------------------


class TestBuildPlaceholders:
    def _make_instance(self, *, port=None, kwargs=None):
        cfg = McpServerConfig(type="script", command="npx", port=port or 9100)
        return McpInstance(
            server_name="browser",
            server_config=cfg,
            kwargs=kwargs or {},
            instance_id="browser",
            container_name="pynchy-mcp-browser",
            port=port,
        )

    def test_includes_port(self):
        inst = self._make_instance(port=9101)
        placeholders = _build_placeholders(inst)
        assert placeholders["port"] == "9101"

    def test_includes_kwargs(self):
        inst = self._make_instance(port=9100, kwargs={"workspace": "sandbox1"})
        placeholders = _build_placeholders(inst)
        assert placeholders["workspace"] == "sandbox1"
        assert placeholders["port"] == "9100"

    def test_no_port_when_none(self):
        inst = self._make_instance(port=None)
        placeholders = _build_placeholders(inst)
        assert "port" not in placeholders


class TestMcpOneCliConfig:
    def test_mcp_server_accepts_onecli_opt_in(self):
        cfg = McpServerConfig(type="docker", image="img", port=8000, onecli=True)

        assert cfg.onecli is True
        assert cfg.onecli_agent == "workspace"

    def test_build_env_args_merges_onecli_env(self):
        cfg = McpServerConfig(
            type="docker",
            image="img",
            port=8000,
            env={"STATIC": "value"},
        )

        args = build_env_args(cfg, extra_env={"HTTPS_PROXY": "http://proxy"})

        assert "-e" in args
        assert "STATIC=value" in args
        assert "HTTPS_PROXY=http://proxy" in args


class TestDockerLifecycleHelpers:
    def _make_instance(
        self,
        *,
        port: int | None = 9100,
        extra_ports: list[int] | None = None,
        volumes: list[str] | None = None,
        kwargs: dict[str, str] | None = None,
    ) -> McpInstance:
        cfg = McpServerConfig(
            type="docker",
            image="img",
            port=8000,
            extra_ports=extra_ports or [],
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

    def test_docker_publish_args_include_primary_and_extra_ports(self):
        instance = self._make_instance(port=9100, extra_ports=[9222, 9333])

        assert _docker_publish_args(instance) == [
            "-p",
            "9100:8000",
            "-p",
            "9222:9222",
            "-p",
            "9333:9333",
        ]

    def test_docker_publish_args_omit_primary_when_instance_port_missing(self):
        instance = self._make_instance(port=None, extra_ports=[9222])

        assert _docker_publish_args(instance) == ["-p", "9222:9222"]

    def test_docker_health_url_prefers_localhost_for_published_port(self):
        instance = self._make_instance(port=9100)

        assert _docker_health_url(instance) == "http://localhost:9100"

    def test_docker_health_url_falls_back_to_endpoint_url_without_host_port(self):
        instance = self._make_instance(port=None)

        assert _docker_health_url(instance) == "http://pynchy-mcp-browser:8000"

    def test_docker_volume_args_expand_workspace_relative_paths(self, tmp_path, monkeypatch):
        instance = self._make_instance(
            volumes=["groups/{workspace}:/workspace", "mcp-cache:/cache"],
            kwargs={"workspace": "research"},
        )
        settings = make_settings(project_root=tmp_path)
        monkeypatch.setattr("pynchy.config.get_settings", lambda: settings)

        args = _docker_volume_args(
            instance,
            _build_placeholders(instance),
            onecli_material=None,
        )

        assert args == [
            "-v",
            f"{tmp_path / 'groups' / 'research'}:/workspace",
            "-v",
            "mcp-cache:/cache",
        ]


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

    def _make_manager(self, workspaces: dict, tool_mcp_configs: dict):
        ws_configs = {}
        profiles = {}
        for name, servers in workspaces.items():
            profile_name = f"{name}-mcp"
            profiles[profile_name] = ProfileConfig(tools=servers)
            ws_configs[name] = WorkspaceConfig(profiles=[profile_name])

        settings = validate_settings_mapping(
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
        gateway = MagicMock(spec=LiteLLMGateway)
        return McpManager(settings, gateway)

    def test_inject_workspace_two_workspaces_get_different_ports(self):
        mgr = self._make_manager(
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
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
        ports = sorted(inst.port for inst in state.instances.values())
        assert ports == [9100, 9101]

    def test_single_workspace_gets_base_port(self):
        mgr = self._make_manager(
            workspaces={"ws1": ["browser"]},
            tool_mcp_configs={
                "browser": {
                    "runtime": "script",
                    "command": "npx",
                    "port": 9100,
                },
            },
        )
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
        inst = next(iter(state.instances.values()))
        assert inst.port == 9100

    def test_dynamic_thread_uses_parent_workspace_instances(self):
        mgr = self._make_manager(
            workspaces={"admin": ["browser"]},
            tool_mcp_configs={
                "browser": {
                    "runtime": "script",
                    "command": "npx",
                    "port": 9100,
                },
            },
        )
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
        mgr._instances = state.instances
        mgr._workspace_instances = state.workspace_instances

        parent_ids = mgr.get_workspace_instance_ids("admin")
        child_ids = mgr.get_workspace_instance_ids("admin__thread_discord-channel-thread")

        assert child_ids == parent_ids
        assert child_ids

    def test_inject_workspace_independent_port_counters_per_server(self):
        mgr = self._make_manager(
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
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
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
        mgr = self._make_manager(
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
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
        # Same instance ID (no kwargs → no hash), so only one instance
        assert len(state.instances) == 1
        inst = next(iter(state.instances.values()))
        assert inst.port == 7000

    def test_url_type_gets_none_port(self):
        mgr = self._make_manager(
            workspaces={"ws1": ["remote"]},
            tool_mcp_configs={
                "remote": {
                    "runtime": "url",
                    "url": "https://example.com/mcp",
                },
            },
        )
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
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
