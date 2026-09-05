"""Tests for per-instance port allocation and arg placeholder expansion."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - test fixtures construct completed Docker command results.
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.config.api import validate_settings_mapping
from pynchy.host.container_manager.docker import HealthCheckRequest
from pynchy.host.container_manager.mcp.lifecycle import (
    build_env_args,
    ensure_docker_running,
    ensure_process_running,
    expand_arg_placeholders,
    reap_stale_processes,
    warm_image_cache,
)
from pynchy.host.container_manager.mcp.resolution import (
    McpInstance,
    merged_mcp_servers,
)
from pynchy.plugins.api import McpServerConfig

ALL_INTERFACE_BIND_HOST = "0.0.0.0"  # noqa: S104 - test fixture for pass-through MCP args that intentionally contain bind-all data.

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
        args = build_env_args({"STATIC": "value"})

        assert args == ["-e", "STATIC"]
        assert "value" not in args

    def test_stdio_server_requires_http_transport(self):
        config = McpServerConfig(
            type="stdio",
            command="npx",
            port=8000,
            transport="streamable_http",
        )

        assert config.type == "stdio"
        with pytest.raises(ValueError, match="Stdio MCP servers require HTTP transport"):
            McpServerConfig(type="stdio", command="npx", port=8000)

    @pytest.mark.parametrize(
        ("config", "message"),
        [
            pytest.param(
                {"type": "docker", "port": 8000},
                "Docker MCP servers require 'image'",
                id="docker-image",
            ),
            pytest.param(
                {"type": "docker", "image": "example"},
                "Docker MCP servers require 'port'",
                id="docker-port",
            ),
            pytest.param(
                {"type": "url"},
                "URL MCP servers require 'url'",
                id="url",
            ),
            pytest.param(
                {"type": "script", "port": 8000},
                "Script MCP servers require 'command'",
                id="script-command",
            ),
            pytest.param(
                {"type": "script", "command": "run"},
                "Script MCP servers require 'port'",
                id="script-port",
            ),
            pytest.param(
                {"type": "stdio", "port": 8000, "transport": "http"},
                "Stdio MCP servers require 'command'",
                id="stdio-command",
            ),
            pytest.param(
                {"type": "stdio", "command": "run", "transport": "http"},
                "Stdio MCP servers require 'port'",
                id="stdio-port",
            ),
        ],
    )
    def test_mcp_server_rejects_incomplete_runtime_configuration(self, config, message):
        with pytest.raises(ValueError, match=message):
            McpServerConfig(**config)


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
        env: dict[str, str] | None = None,
        tool_environment: dict[str, str] | None = None,
        project_root: Path = Path("/project"),
    ) -> McpInstance:
        cfg = McpServerConfig(
            type="docker",
            image="img",
            port=8000,
            extra_ports=extra_ports or [],
            args=args or [],
            volumes=volumes or [],
            env=env or {},
        )
        return McpInstance(
            server_name="browser",
            server_config=cfg,
            kwargs=kwargs or {},
            instance_id="browser",
            container_name="pynchy-mcp-browser",
            project_root=project_root,
            port=port,
            tool_environment=tool_environment or {},
        )

    @pytest.mark.asyncio
    async def test_warm_image_cache_uses_instance_project_root(self, tmp_path, monkeypatch):
        instance = self._make_instance(project_root=tmp_path)
        ensure_image_mock = AsyncMock()
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle._ensure_mcp_image",
            ensure_image_mock,
        )

        await warm_image_cache({"browser": instance})

        ensure_image_mock.assert_awaited_once_with(instance.server_config, tmp_path)

    @pytest.mark.asyncio
    async def test_ensure_docker_running_preserves_an_existing_container(self, monkeypatch):
        instance = self._make_instance()
        run_docker_mock = AsyncMock()
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.is_container_running",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.run_docker",
            run_docker_mock,
        )

        await ensure_docker_running(instance)

        run_docker_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ensure_docker_running_builds_expanded_docker_run_and_localhost_health_check(
        self,
        tmp_path,
        monkeypatch,
    ):
        instance = self._make_instance(
            port=9100,
            extra_ports=[9222, 9333],
            volumes=[
                "groups/{workspace}:/workspace",
                "mcp-cache:/cache",
                f"{tmp_path / 'absolute'}:/absolute",
            ],
            args=["--workspace-dir", "{workspace}", "--port", "{port}"],
            kwargs={"workspace": "research"},
            project_root=tmp_path,
        )

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
            "-v",
            f"{tmp_path / 'absolute'}:/absolute",
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
    async def test_docker_passes_selected_values_only_through_the_client_environment(
        self,
        monkeypatch,
    ):
        instance = self._make_instance(
            env={"STATIC_SETTING": "static-value"},
            tool_environment={"SELECTED_TOKEN": "selected-value"},
        )
        monkeypatch.setenv("UNRELATED_HOST_TOKEN", "must-not-leak")
        run_docker_mock, _wait_healthy_mock = self._stub_docker_lifecycle(monkeypatch)

        await ensure_docker_running(instance)

        argv = run_docker_mock.await_args.args
        environment = run_docker_mock.await_args.kwargs["environment"]
        assert argv[argv.index("-e") :] == (
            "-e",
            "SELECTED_TOKEN",
            "-e",
            "STATIC_SETTING",
            "img",
        )
        assert "selected-value" not in argv
        assert "static-value" not in argv
        assert environment["SELECTED_TOKEN"] == "selected-value"  # noqa: S105  # pragma: allowlist secret
        assert environment["STATIC_SETTING"] == "static-value"
        assert "UNRELATED_HOST_TOKEN" not in environment

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


class TestStdioLifecycle:
    @pytest.mark.asyncio
    async def test_ensure_stdio_requires_an_allocated_host_port(self):
        instance = McpInstance(
            server_name="android",
            server_config=McpServerConfig(
                type="stdio",
                command="npx",
                port=8932,
                transport="streamable_http",
            ),
            kwargs={},
            instance_id="android",
            container_name="unused",
            project_root=Path("/project"),
            port=None,
        )

        with pytest.raises(RuntimeError, match="Stdio MCP has no host port: android"):
            await ensure_process_running(instance)

    @pytest.mark.asyncio
    async def test_ensure_stdio_runs_loopback_bridge_with_filtered_environment(self, monkeypatch):
        instance = McpInstance(
            server_name="android",
            server_config=McpServerConfig(
                type="stdio",
                command="npx",
                args=["-y", "scrcpy-mcp", "--port", "{port}"],
                port=8932,
                transport="streamable_http",
                env={"ADB_PATH": "/opt/homebrew/bin/adb"},
            ),
            kwargs={},
            instance_id="android",
            container_name="unused",
            project_root=Path("/project"),
            port=8932,
            tool_environment={"ANDROID_TOKEN": "selected-value"},
        )
        start_process = MagicMock(return_value=None)
        wait_healthy_mock = AsyncMock()
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("SERVICE_TOKEN", "not-forwarded")
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle._start_script_process",
            start_process,
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.wait_healthy",
            wait_healthy_mock,
        )

        await ensure_process_running(instance)

        command, environment, marker = start_process.call_args.args
        assert command == [
            sys.executable,
            "-m",
            "pynchy.host.container_manager.mcp.stdio_bridge",
            "--port",
            "8932",
            "--",
            "npx",
            "-y",
            "scrcpy-mcp",
            "--port",
            "8932",
        ]
        assert environment["ADB_PATH"] == "/opt/homebrew/bin/adb"
        assert environment["ANDROID_TOKEN"] == "selected-value"  # noqa: S105  # pragma: allowlist secret
        assert environment["PATH"] == "/usr/bin"
        assert "SERVICE_TOKEN" not in environment
        assert marker.startswith("pynchy-mcp-")
        assert wait_healthy_mock.await_args.args == (
            HealthCheckRequest(
                container_name="android",
                url="http://localhost:8932",
                any_non_5xx=True,
                process=None,
                health_timeout_seconds=5.0,
            ),
        )


class TestScriptLifecycle:
    @pytest.mark.asyncio
    async def test_ensure_process_running_preserves_a_live_process(self, monkeypatch):
        process = subprocess.Popen.__new__(subprocess.Popen)
        process.poll = MagicMock(return_value=None)
        instance = McpInstance(
            server_name="linear",
            server_config=McpServerConfig(
                type="script",
                command="uv",
                port=8474,
                transport="streamable_http",
            ),
            kwargs={},
            instance_id="linear",
            container_name="unused",
            project_root=Path("/project"),
            port=8474,
            process=process,
        )
        start_process = MagicMock()
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle._start_script_process",
            start_process,
        )

        await ensure_process_running(instance)

        start_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_script_cleans_up_after_failed_health_check(self, monkeypatch):
        instance = McpInstance(
            server_name="linear",
            server_config=McpServerConfig(
                type="script",
                command="uv",
                port=8474,
                transport="streamable_http",
            ),
            kwargs={},
            instance_id="linear",
            container_name="unused",
            project_root=Path("/project"),
            port=8474,
        )
        process = subprocess.Popen.__new__(subprocess.Popen)
        terminate = MagicMock(side_effect=lambda current: setattr(current, "process", None))
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle._start_script_process",
            MagicMock(return_value=process),
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.wait_healthy",
            AsyncMock(side_effect=TimeoutError("not ready")),
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.terminate_process",
            terminate,
        )

        with pytest.raises(TimeoutError, match="not ready"):
            await ensure_process_running(instance)

        terminate.assert_called_once_with(instance)
        assert instance.process is None

    @pytest.mark.asyncio
    async def test_ensure_script_runs_with_filtered_environment(self, monkeypatch, tmp_path):
        record_path = tmp_path / "processes" / "linear.json"
        instance = McpInstance(
            server_name="linear",
            server_config=McpServerConfig(
                type="script",
                command="uv",
                args=["run", "linear-server", "--port", "{port}"],
                port=8474,
                transport="streamable_http",
                env={"STATIC_SETTING": "static-value"},
            ),
            kwargs={},
            instance_id="linear",
            container_name="unused",
            project_root=Path("/project"),
            port=8474,
            tool_environment={"LINEAR_API_KEY": "selected-value"},  # pragma: allowlist secret
            process_record_path=record_path,
        )
        process = subprocess.Popen.__new__(subprocess.Popen)
        process.pid = 123
        start_process = MagicMock(return_value=process)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("UNRELATED_HOST_TOKEN", "must-not-leak")
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle._start_script_process",
            start_process,
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle.wait_healthy",
            AsyncMock(),
        )

        await ensure_process_running(instance)

        command, environment, marker = start_process.call_args.args
        assert command == ["uv", "run", "linear-server", "--port", "8474"]
        assert environment["LINEAR_API_KEY"] == "selected-value"  # pragma: allowlist secret
        assert environment["STATIC_SETTING"] == "static-value"
        assert environment["PATH"] == "/usr/bin"
        assert "UNRELATED_HOST_TOKEN" not in environment
        assert json.loads(record_path.read_text()) == {
            "version": 1,
            "pid": 123,
            "marker": marker,
            "instance_id": "linear",
        }

    def test_reap_stale_processes_signals_only_marker_verified_groups(self, monkeypatch, tmp_path):
        record_dir = tmp_path / "mcp-processes"
        record_dir.mkdir()
        owned = record_dir / "owned.json"
        stale = record_dir / "stale.json"
        owned.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 123,
                    "marker": "pynchy-mcp-" + ("a" * 32),
                    "instance_id": "linear",
                }
            )
        )
        stale.write_text(json.dumps({"pid": 456, "marker": "not-owned"}))
        terminated: list[int] = []
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle._process_has_marker",
            lambda pid, marker: pid == 123 and marker.endswith("a" * 32),
        )
        monkeypatch.setattr(
            "pynchy.host.container_manager.mcp.lifecycle._terminate_owned_process_group",
            terminated.append,
        )

        assert reap_stale_processes(record_dir) == 1
        assert terminated == [123]
        assert list(record_dir.iterdir()) == []


class TestMergedMcpServers:
    def test_config_base_override_preserves_plugin_dockerfile_when_omitted(self):
        plugin_servers = {
            "gdrive": McpServerConfig(
                type="docker",
                image="pynchy-mcp-gdrive:latest",
                dockerfile="src/pynchy/agent/mcp/gdrive.Dockerfile",
                build_context="src/pynchy/agent/mcp",
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
        assert merged["gdrive"].build_context == "src/pynchy/agent/mcp"
        assert merged["gdrive"].port == 3000
        assert merged["gdrive"].env == {
            "GDRIVE_CREDENTIALS_PATH": "/gdrive-server/credentials.json"
        }
        assert merged["gdrive"].volumes == [
            "mcp-gdrive:/gdrive-server",
            "data/gcp-oauth.keys.json:/app/gcp-oauth.keys.json:ro",
        ]
