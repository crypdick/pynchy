"""Tests for the container runtime abstraction."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

import pynchy.runtime as runtime_mod
from pynchy.config import (
    AgentConfig,
    CommandWordsConfig,
    ContainerConfig,
    IntervalsConfig,
    LoggingConfig,
    QueueConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    Settings,
    WorkspaceDefaultsConfig,
)
from pynchy.runtime import ContainerRuntime, detect_runtime


def _settings(*, runtime_override: str | None = None) -> Settings:
    return Settings.model_construct(
        agent=AgentConfig(),
        container=ContainerConfig(runtime=runtime_override),
        server=ServerConfig(),
        logging=LoggingConfig(),
        secrets=SecretsConfig(),
        workspace_defaults=WorkspaceDefaultsConfig(),
        workspaces={},
        commands=CommandWordsConfig(),
        scheduler=SchedulerConfig(),
        intervals=IntervalsConfig(),
        queue=QueueConfig(),
        security=SecurityConfig(),
    )


class TestDetectRuntime:
    def test_settings_override_apple(self):
        with patch("pynchy.config.get_settings", return_value=_settings(runtime_override="apple")):
            r = detect_runtime()
        assert r.name == "apple"
        assert r.cli == "container"

    def test_settings_override_docker(self):
        with patch("pynchy.config.get_settings", return_value=_settings(runtime_override="docker")):
            r = detect_runtime()
        assert r.name == "docker"
        assert r.cli == "docker"

    def test_darwin_prefers_apple_container(self):
        with (
            patch("pynchy.config.get_settings", return_value=_settings(runtime_override=None)),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which", return_value="/usr/bin/container"),
        ):
            mock_sys.platform = "darwin"
            r = detect_runtime()
        assert r.name == "apple"

    def test_linux_uses_docker(self):
        with (
            patch("pynchy.config.get_settings", return_value=_settings(runtime_override=None)),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which") as mock_which,
        ):
            mock_sys.platform = "linux"
            mock_which.side_effect = lambda cmd: "/usr/bin/docker" if cmd == "docker" else None
            r = detect_runtime()
        assert r.name == "docker"

    def test_fallback_when_nothing_found(self):
        with (
            patch("pynchy.config.get_settings", return_value=_settings(runtime_override=None)),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which", return_value=None),
        ):
            mock_sys.platform = "linux"
            r = detect_runtime()
        assert r.name == "docker"


class TestListAppleContainers:
    def test_parses_apple_json_format(self):
        rt = ContainerRuntime(name="apple", cli="container")
        apple_json = json.dumps(
            [
                {"status": "running", "configuration": {"id": "pynchy-group1-123"}},
                {"status": "stopped", "configuration": {"id": "pynchy-group2-456"}},
                {"status": "running", "configuration": {"id": "other-container"}},
            ]
        )
        with patch("pynchy.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = apple_json
            result = rt.list_running_containers("pynchy-")
        assert result == ["pynchy-group1-123"]


class TestListDockerContainers:
    def test_parses_docker_ndjson_format(self):
        rt = ContainerRuntime(name="docker", cli="docker")
        ndjson = "\n".join(
            [
                json.dumps({"Names": "pynchy-group1-123"}),
                json.dumps({"Names": "pynchy-group2-456"}),
                json.dumps({"Names": "other-container"}),
            ]
        )
        with patch("pynchy.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ndjson
            result = rt.list_running_containers("pynchy-")
        assert result == ["pynchy-group1-123", "pynchy-group2-456"]

    def test_handles_empty_output(self):
        rt = ContainerRuntime(name="docker", cli="docker")
        with patch("pynchy.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            result = rt.list_running_containers("pynchy-")
        assert result == []


class TestEnsureRunning:
    def test_docker_calls_docker_info(self):
        rt = ContainerRuntime(name="docker", cli="docker")
        with patch("pynchy.runtime.subprocess.run") as mock_run:
            rt.ensure_running()
        mock_run.assert_called_once_with(["docker", "info"], capture_output=True, check=True)

    def test_apple_calls_system_status(self):
        rt = ContainerRuntime(name="apple", cli="container")
        with patch("pynchy.runtime.subprocess.run") as mock_run:
            rt.ensure_running()
        mock_run.assert_called_once_with(
            ["container", "system", "status"], capture_output=True, check=True
        )


class TestGetRuntime:
    def test_caches_result(self):
        runtime_mod._runtime = None
        try:
            with patch("pynchy.runtime.detect_runtime") as mock_detect:
                mock_detect.return_value = ContainerRuntime(name="docker", cli="docker")
                r1 = runtime_mod.get_runtime()
                r2 = runtime_mod.get_runtime()
            assert r1 is r2
            mock_detect.assert_called_once()
        finally:
            runtime_mod._runtime = None


class TestDetectRuntimeEdgeCases:
    def test_darwin_falls_back_to_docker_when_no_apple_container(self):
        def which_side_effect(cmd):
            return "/usr/local/bin/docker" if cmd == "docker" else None

        with (
            patch("pynchy.config.get_settings", return_value=_settings(runtime_override=None)),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which", side_effect=which_side_effect),
        ):
            mock_sys.platform = "darwin"
            r = detect_runtime()
        assert r.name == "docker"

    def test_darwin_fallback_when_nothing_installed(self):
        with (
            patch("pynchy.config.get_settings", return_value=_settings(runtime_override=None)),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which", return_value=None),
        ):
            mock_sys.platform = "darwin"
            r = detect_runtime()
        assert r.name == "apple"

    def test_unknown_runtime_override_falls_through(self):
        with (
            patch("pynchy.config.get_settings", return_value=_settings(runtime_override="podman")),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which") as mock_which,
        ):
            mock_sys.platform = "linux"
            mock_which.return_value = "/usr/bin/docker"
            r = detect_runtime()
        assert r.name == "docker"


class TestEnsureRunningErrors:
    def test_docker_not_running_on_linux_raises(self):
        rt = ContainerRuntime(name="docker", cli="docker")
        with (
            patch(
                "pynchy.runtime.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "docker"),
            ),
            patch("pynchy.runtime.sys") as mock_sys,
            pytest.raises(RuntimeError, match="systemctl"),
        ):
            mock_sys.platform = "linux"
            rt.ensure_running()

    def test_apple_system_start_failure_raises(self):
        rt = ContainerRuntime(name="apple", cli="container")

        def mock_run(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "container")

        with (
            patch("pynchy.runtime.subprocess.run", side_effect=mock_run),
            pytest.raises(RuntimeError, match="Apple Container"),
        ):
            rt.ensure_running()
