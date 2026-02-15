"""Tests for the container runtime abstraction."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

import pynchy.runtime as runtime_mod
from pynchy.runtime import ContainerRuntime, detect_runtime


class TestDetectRuntime:
    def test_env_var_override_apple(self):
        with patch.dict("os.environ", {"CONTAINER_RUNTIME": "apple"}):
            r = detect_runtime()
        assert r.name == "apple"
        assert r.cli == "container"

    def test_env_var_override_docker(self):
        with patch.dict("os.environ", {"CONTAINER_RUNTIME": "docker"}):
            r = detect_runtime()
        assert r.name == "docker"
        assert r.cli == "docker"

    def test_darwin_prefers_apple_container(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which", return_value="/usr/bin/container"),
        ):
            mock_sys.platform = "darwin"
            r = detect_runtime()
        assert r.name == "apple"

    def test_linux_uses_docker(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which") as mock_which,
        ):
            mock_sys.platform = "linux"
            mock_which.side_effect = lambda cmd: "/usr/bin/docker" if cmd == "docker" else None
            r = detect_runtime()
        assert r.name == "docker"

    def test_fallback_when_nothing_found(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which", return_value=None),
        ):
            mock_sys.platform = "linux"
            r = detect_runtime()
        # Falls back to docker on non-Darwin
        assert r.name == "docker"


class TestListAppleContainers:
    def test_parses_apple_json_format(self):
        rt = ContainerRuntime(name="apple", cli="container")
        apple_json = json.dumps(
            [
                {
                    "status": "running",
                    "configuration": {"id": "pynchy-group1-123"},
                },
                {
                    "status": "stopped",
                    "configuration": {"id": "pynchy-group2-456"},
                },
                {
                    "status": "running",
                    "configuration": {"id": "other-container"},
                },
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
        mock_run.assert_called_once_with(
            ["docker", "info"],
            capture_output=True,
            check=True,
        )

    def test_apple_calls_system_status(self):
        rt = ContainerRuntime(name="apple", cli="container")
        with patch("pynchy.runtime.subprocess.run") as mock_run:
            rt.ensure_running()
        mock_run.assert_called_once_with(
            ["container", "system", "status"],
            capture_output=True,
            check=True,
        )


class TestGetRuntime:
    def test_caches_result(self):
        # Reset singleton
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
    """Edge cases in runtime detection: Darwin Docker fallback, unknown env var."""

    def test_darwin_falls_back_to_docker_when_no_apple_container(self):
        """On macOS without Apple Container, falls back to Docker if available."""
        def which_side_effect(cmd):
            return "/usr/local/bin/docker" if cmd == "docker" else None

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which", side_effect=which_side_effect),
        ):
            mock_sys.platform = "darwin"
            r = detect_runtime()
        assert r.name == "docker"
        assert r.cli == "docker"

    def test_darwin_fallback_when_nothing_installed(self):
        """On macOS with neither runtime installed, defaults to apple."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which", return_value=None),
        ):
            mock_sys.platform = "darwin"
            r = detect_runtime()
        assert r.name == "apple"

    def test_env_var_case_insensitive(self):
        """CONTAINER_RUNTIME env var should be case-insensitive."""
        with patch.dict("os.environ", {"CONTAINER_RUNTIME": "DOCKER"}):
            r = detect_runtime()
        assert r.name == "docker"

        with patch.dict("os.environ", {"CONTAINER_RUNTIME": "Apple"}):
            r = detect_runtime()
        assert r.name == "apple"

    def test_unknown_env_var_falls_through(self):
        """Unknown CONTAINER_RUNTIME value falls through to platform detection."""
        with (
            patch.dict("os.environ", {"CONTAINER_RUNTIME": "podman"}),
            patch("pynchy.runtime.sys") as mock_sys,
            patch("pynchy.runtime.shutil.which") as mock_which,
        ):
            mock_sys.platform = "linux"
            mock_which.return_value = "/usr/bin/docker"
            r = detect_runtime()
        # Should fall through to docker detection
        assert r.name == "docker"


class TestEnsureRunningErrors:
    """Tests for error paths in ensure_running."""

    def test_docker_not_running_on_linux_raises(self):
        """On Linux, Docker not running should raise RuntimeError."""
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
        """Apple Container system start failure should raise RuntimeError."""
        rt = ContainerRuntime(name="apple", cli="container")

        def mock_run(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "container")

        with (
            patch("pynchy.runtime.subprocess.run", side_effect=mock_run),
            pytest.raises(RuntimeError, match="Apple Container"),
        ):
            rt.ensure_running()

    def test_apple_system_already_running(self):
        """Apple Container system already running should not attempt start."""
        rt = ContainerRuntime(name="apple", cli="container")
        with patch("pynchy.runtime.subprocess.run") as mock_run:
            rt.ensure_running()
        # Only status check should be called
        mock_run.assert_called_once_with(
            ["container", "system", "status"],
            capture_output=True,
            check=True,
        )

    def test_apple_system_starts_when_not_running(self):
        """Apple Container system should be started when status check fails."""
        rt = ContainerRuntime(name="apple", cli="container")
        call_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if "status" in cmd:
                raise subprocess.CalledProcessError(1, "container")
            # start succeeds
            return subprocess.CompletedProcess(cmd, 0)

        with patch("pynchy.runtime.subprocess.run", side_effect=mock_run):
            rt.ensure_running()

        assert call_count == 2  # status + start


class TestListContainerErrors:
    """Tests for list_running_containers error handling."""

    def test_returns_empty_on_exception(self):
        """list_running_containers returns [] on any subprocess error."""
        rt = ContainerRuntime(name="docker", cli="docker")
        with patch("pynchy.runtime.subprocess.run", side_effect=Exception("oops")):
            result = rt.list_running_containers()
        assert result == []

    def test_apple_handles_empty_json(self):
        """Apple container list handles empty JSON array."""
        rt = ContainerRuntime(name="apple", cli="container")
        with patch("pynchy.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "[]"
            result = rt.list_running_containers()
        assert result == []

    def test_docker_handles_blank_lines(self):
        """Docker container list handles blank lines in output."""
        rt = ContainerRuntime(name="docker", cli="docker")
        ndjson = f'{json.dumps({"Names": "pynchy-test-123"})}\n\n\n'
        with patch("pynchy.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ndjson
            result = rt.list_running_containers("pynchy-")
        assert result == ["pynchy-test-123"]

    def test_custom_prefix_filter(self):
        """list_running_containers filters by custom prefix."""
        rt = ContainerRuntime(name="docker", cli="docker")
        ndjson = "\n".join([
            json.dumps({"Names": "custom-app-1"}),
            json.dumps({"Names": "pynchy-test"}),
            json.dumps({"Names": "custom-app-2"}),
        ])
        with patch("pynchy.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ndjson
            result = rt.list_running_containers("custom-")
        assert result == ["custom-app-1", "custom-app-2"]
