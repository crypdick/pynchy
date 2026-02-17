"""Tests for the container runtime abstraction."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest
from conftest import make_settings

import pynchy.runtime.runtime as runtime_mod
from pynchy.config import ContainerConfig
from pynchy.runtime.runtime import detect_runtime


def _settings(*, runtime_override: str | None = None):
    return make_settings(container=ContainerConfig(runtime=runtime_override))


class FakePluginRuntime:
    def __init__(self, *, name: str, available: bool = True):
        self.name = name
        self.cli = "container"
        self._available = available

    def is_available(self) -> bool:
        return self._available

    def ensure_running(self) -> None:  # pragma: no cover - not used here
        return None

    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]:  # pragma: no cover
        return []


class TestDetectRuntime:
    def test_settings_override_apple_uses_plugin_runtime(self):
        apple = FakePluginRuntime(name="apple")
        with (
            patch("pynchy.config.get_settings", return_value=_settings(runtime_override="apple")),
            patch("pynchy.runtime.runtime._iter_plugin_runtimes", return_value=[apple]),
        ):
            r = detect_runtime()
        assert r is apple

    def test_settings_override_docker(self):
        with patch("pynchy.config.get_settings", return_value=_settings(runtime_override="docker")):
            r = detect_runtime()
        assert r.name == "docker"
        assert r.cli == "docker"

    def test_darwin_prefers_apple_plugin_runtime(self):
        apple = FakePluginRuntime(name="apple")
        with (
            patch("pynchy.config.get_settings", return_value=_settings(runtime_override=None)),
            patch("pynchy.runtime.runtime._iter_plugin_runtimes", return_value=[apple]),
            patch("pynchy.runtime.runtime.sys") as mock_sys,
            patch("pynchy.runtime.runtime.shutil.which", return_value="/usr/bin/docker"),
        ):
            mock_sys.platform = "darwin"
            r = detect_runtime()
        assert r is apple

    def test_darwin_without_apple_plugin_uses_docker(self):
        with (
            patch("pynchy.config.get_settings", return_value=_settings(runtime_override=None)),
            patch("pynchy.runtime.runtime._iter_plugin_runtimes", return_value=[]),
            patch("pynchy.runtime.runtime.sys") as mock_sys,
            patch("pynchy.runtime.runtime.shutil.which", return_value="/usr/bin/docker"),
        ):
            mock_sys.platform = "darwin"
            r = detect_runtime()
        assert r.name == "docker"

    def test_unknown_runtime_override_falls_back_to_docker(self):
        with (
            patch("pynchy.config.get_settings", return_value=_settings(runtime_override="podman")),
            patch("pynchy.runtime.runtime._iter_plugin_runtimes", return_value=[]),
            patch("pynchy.runtime.runtime.sys") as mock_sys,
            patch("pynchy.runtime.runtime.shutil.which", return_value="/usr/bin/docker"),
        ):
            mock_sys.platform = "linux"
            r = detect_runtime()
        assert r.name == "docker"


class TestDockerRuntime:
    def test_parses_docker_ndjson_format(self):
        rt = runtime_mod._docker_runtime()
        ndjson = "\n".join(
            [
                json.dumps({"Names": "pynchy-group1-123"}),
                json.dumps({"Names": "pynchy-group2-456"}),
                json.dumps({"Names": "other-container"}),
            ]
        )
        with patch("pynchy.runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ndjson
            result = rt.list_running_containers("pynchy-")
        assert result == ["pynchy-group1-123", "pynchy-group2-456"]

    def test_handles_empty_output(self):
        rt = runtime_mod._docker_runtime()
        with patch("pynchy.runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            result = rt.list_running_containers("pynchy-")
        assert result == []

    def test_ensure_running_calls_docker_info(self):
        rt = runtime_mod._docker_runtime()
        with patch("pynchy.runtime.runtime.subprocess.run") as mock_run:
            rt.ensure_running()
        mock_run.assert_called_once_with(["docker", "info"], capture_output=True, check=True)

    def test_docker_not_running_on_linux_raises(self):
        rt = runtime_mod._docker_runtime()
        with (
            patch(
                "pynchy.runtime.runtime.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "docker"),
            ),
            patch("pynchy.runtime.runtime.sys") as mock_sys,
            pytest.raises(RuntimeError, match="systemctl"),
        ):
            mock_sys.platform = "linux"
            rt.ensure_running()


class TestGetRuntime:
    def test_caches_result(self):
        runtime_mod._runtime = None
        try:
            with patch("pynchy.runtime.runtime.detect_runtime") as mock_detect:
                mock_detect.return_value = runtime_mod._docker_runtime()
                r1 = runtime_mod.get_runtime()
                r2 = runtime_mod.get_runtime()
            assert r1 is r2
            mock_detect.assert_called_once()
        finally:
            runtime_mod._runtime = None
