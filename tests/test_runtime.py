"""Tests for the container runtime abstraction."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - test helpers mock subprocess behavior and exceptions
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_settings

import pynchy.plugins.runtimes.detection as runtime_mod
from pynchy.config.api import ContainerConfig
from pynchy.plugins.runtimes.apple_runtime.runtime import AppleContainerRuntime
from pynchy.plugins.runtimes.cleanup import OrphanReapingRuntime
from pynchy.plugins.runtimes.detection import detect_runtime
from pynchy.plugins.runtimes.docker_runtime.runtime import DockerContainerRuntime


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


def _docker_plugin():
    """Return a Docker plugin runtime for tests."""
    return DockerContainerRuntime()


class TestDetectRuntime:
    def test_settings_override_apple_uses_plugin_runtime(self):
        apple = FakePluginRuntime(name="apple")
        docker = _docker_plugin()
        with (
            patch(
                "pynchy.plugins.runtimes.detection._iter_plugin_runtimes",
                return_value=[apple, docker],
            ),
        ):
            r = detect_runtime("apple")
        assert r is apple

    def test_settings_override_docker(self):
        docker = _docker_plugin()
        with (
            patch("pynchy.plugins.runtimes.detection._iter_plugin_runtimes", return_value=[docker]),
        ):
            r = detect_runtime("docker")
        assert r.name == "docker"
        assert r.cli == "docker"

    def test_blank_and_duplicate_runtime_names_are_ignored(self):
        apple = FakePluginRuntime(name="apple")
        duplicate = FakePluginRuntime(name=" apple ")
        with patch(
            "pynchy.plugins.runtimes.detection._iter_plugin_runtimes",
            return_value=[FakePluginRuntime(name=""), apple, duplicate],
        ):
            assert detect_runtime("apple") is apple

    def test_darwin_falls_back_to_docker_when_apple_is_unavailable(self):
        apple = FakePluginRuntime(name="apple", available=False)
        docker = FakePluginRuntime(name="docker")
        with (
            patch(
                "pynchy.plugins.runtimes.detection._iter_plugin_runtimes",
                return_value=[apple, docker],
            ),
            patch("pynchy.plugins.runtimes.detection.sys") as mock_sys,
        ):
            mock_sys.platform = "darwin"
            assert detect_runtime() is docker

    def test_linux_falls_back_to_an_available_non_docker_runtime(self):
        custom = FakePluginRuntime(name="podman")
        with (
            patch(
                "pynchy.plugins.runtimes.detection._iter_plugin_runtimes",
                return_value=[custom],
            ),
            patch("pynchy.plugins.runtimes.detection.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            assert detect_runtime() is custom

    def test_darwin_prefers_apple_plugin_runtime(self):
        apple = FakePluginRuntime(name="apple")
        docker = _docker_plugin()
        with (
            patch(
                "pynchy.plugins.runtimes.detection._iter_plugin_runtimes",
                return_value=[apple, docker],
            ),
            patch("pynchy.plugins.runtimes.detection.sys") as mock_sys,
        ):
            mock_sys.platform = "darwin"
            r = detect_runtime()
        assert r is apple

    def test_darwin_without_apple_plugin_uses_docker(self):
        docker = _docker_plugin()
        with (
            patch("pynchy.plugins.runtimes.detection._iter_plugin_runtimes", return_value=[docker]),
            patch("pynchy.plugins.runtimes.detection.sys") as mock_sys,
        ):
            mock_sys.platform = "darwin"
            r = detect_runtime()
        assert r.name == "docker"

    def test_darwin_returns_unavailable_apple_when_docker_is_unavailable(self):
        apple = FakePluginRuntime(name="apple", available=False)
        docker = FakePluginRuntime(name="docker", available=False)
        with (
            patch(
                "pynchy.plugins.runtimes.detection._iter_plugin_runtimes",
                return_value=[apple, docker],
            ),
            patch("pynchy.plugins.runtimes.detection.sys") as mock_sys,
        ):
            mock_sys.platform = "darwin"
            assert detect_runtime() is apple

    def test_linux_uses_first_runtime_when_none_are_available(self):
        first = FakePluginRuntime(name="podman", available=False)
        second = FakePluginRuntime(name="nerdctl", available=False)
        with (
            patch(
                "pynchy.plugins.runtimes.detection._iter_plugin_runtimes",
                return_value=[first, second],
            ),
            patch("pynchy.plugins.runtimes.detection.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            assert detect_runtime() is first

    def test_darwin_falls_back_when_only_custom_runtime_is_unavailable(self):
        custom = FakePluginRuntime(name="podman", available=False)
        with (
            patch(
                "pynchy.plugins.runtimes.detection._iter_plugin_runtimes",
                return_value=[custom],
            ),
            patch("pynchy.plugins.runtimes.detection.sys") as mock_sys,
        ):
            mock_sys.platform = "darwin"
            assert detect_runtime() is custom

    def test_unknown_runtime_override_falls_back_to_docker(self):
        docker = _docker_plugin()
        with (
            patch("pynchy.plugins.runtimes.detection._iter_plugin_runtimes", return_value=[docker]),
            patch("pynchy.plugins.runtimes.detection.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            r = detect_runtime("podman")
        assert r.name == "docker"

    def test_no_plugins_raises(self):
        with (
            patch("pynchy.plugins.runtimes.detection._iter_plugin_runtimes", return_value=[]),
            pytest.raises(RuntimeError, match="No container runtime plugins"),
        ):
            detect_runtime()


class TestDockerRuntime:
    def test_satisfies_orphan_reaping_contract(self):
        assert isinstance(DockerContainerRuntime(), OrphanReapingRuntime)

    def test_reports_cli_availability(self):
        runtime = DockerContainerRuntime()
        with patch(
            "pynchy.plugins.runtimes.docker_runtime.runtime.shutil.which",
            side_effect=("/usr/bin/docker", None),
        ):
            assert runtime.is_available() is True
            assert runtime.is_available() is False

    def test_parses_docker_ndjson_format(self):
        rt = DockerContainerRuntime()
        ndjson = "\n".join(
            [
                json.dumps({"Names": "pynchy-group1-123", "State": "running"}),
                json.dumps({"Names": "pynchy-group2-456", "State": "running"}),
                json.dumps({"Names": "other-container", "State": "running"}),
            ]
        )
        with patch("pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ndjson
            result = rt.list_running_containers("pynchy-")
        assert result == ["pynchy-group1-123", "pynchy-group2-456"]

    def test_handles_empty_output(self):
        rt = DockerContainerRuntime()
        with patch("pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            result = rt.list_running_containers("pynchy-")
        assert result == []

    def test_list_containers_skips_blank_records_and_parses_timestamp_fallback(self):
        rt = DockerContainerRuntime()
        record = json.dumps(
            {
                "Names": "pynchy-group",
                "State": "running",
                "CreatedAt": "2026-01-01 12:00:00 +0000",
            }
        )
        ndjson = record + "\n\n" + record
        with patch("pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ndjson

            result = rt.list_containers("pynchy-")

        assert [item.created_at for item in result] == [datetime(2026, 1, 1, 12, tzinfo=UTC)] * 2

    def test_list_containers_marks_agent_by_label_or_legacy_image(self):
        rt = DockerContainerRuntime()
        ndjson = "\n".join(
            [
                json.dumps(
                    {
                        "Names": "pynchy-labeled",
                        "State": "running",
                        "Image": "custom:latest",
                        "Labels": "com.pynchy.role=agent",
                    }
                ),
                json.dumps(
                    {
                        "Names": "pynchy-legacy",
                        "State": "exited",
                        "Image": "pynchy-agent:latest",
                        "Labels": "",
                    }
                ),
                json.dumps(
                    {
                        "Names": "pynchy-litellm",
                        "State": "running",
                        "Image": "ghcr.io/berriai/litellm:main-latest",
                        "Labels": "",
                    }
                ),
            ]
        )
        with patch("pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ndjson
            result = rt.list_containers("pynchy-")

        assert [item.name for item in result if item.is_agent_container] == [
            "pynchy-labeled",
            "pynchy-legacy",
        ]

    def test_list_containers_discards_invalid_timestamp_and_malformed_label(self):
        rt = DockerContainerRuntime()
        record = json.dumps(
            {
                "Names": "pynchy-malformed",
                "State": "created",
                "CreatedAt": "not-a-docker-timestamp",
                "Labels": "malformed-label",
            }
        )
        with patch("pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run") as run:
            run.return_value.stdout = record

            [container] = rt.list_containers()

        assert container.created_at is None
        assert container.labels == {}

    def test_ensure_running_calls_docker_info(self):
        rt = DockerContainerRuntime()
        with patch("pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run") as mock_run:
            rt.ensure_running()
        mock_run.assert_called_once_with(["docker", "info"], capture_output=True, check=True)

    def test_prune_images_prunes_dangling_images(self):
        rt = DockerContainerRuntime()
        with patch("pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            assert rt.prune_images() is True

        mock_run.assert_called_once_with(
            ["docker", "image", "prune", "-f"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def test_prune_images_can_prune_all_unused_images(self):
        rt = DockerContainerRuntime()
        with patch("pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            assert rt.prune_images(all_images=True) is True

        assert mock_run.call_args.args[0] == ["docker", "image", "prune", "-f", "-a"]

    def test_docker_not_running_on_linux_raises(self):
        rt = DockerContainerRuntime()
        with (
            patch(
                "pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "docker"),
            ),
            patch("pynchy.plugins.runtimes.docker_runtime.runtime.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            with pytest.raises(RuntimeError, match="systemctl"):
                rt.ensure_running()

    def test_docker_not_running_on_macos_starts_desktop_then_retries(self):
        runtime = DockerContainerRuntime()
        unavailable = subprocess.CalledProcessError(1, "docker")
        with (
            patch(
                "pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run",
                side_effect=[unavailable, MagicMock(), MagicMock()],
            ) as run,
            patch("pynchy.plugins.runtimes.docker_runtime.runtime.sys") as system,
        ):
            system.platform = "darwin"
            runtime.ensure_running()

        assert run.call_args_list[1].args[0] == ["open", "-a", "Docker"]
        assert run.call_args_list[2].args[0] == ["docker", "info"]

    def test_docker_desktop_start_failure_has_actionable_error(self):
        runtime = DockerContainerRuntime()
        unavailable = subprocess.CalledProcessError(1, "docker")
        with (
            patch(
                "pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run",
                side_effect=[unavailable, FileNotFoundError()],
            ),
            patch("pynchy.plugins.runtimes.docker_runtime.runtime.sys") as system,
        ):
            system.platform = "darwin"
            with pytest.raises(RuntimeError, match="Install from"):
                runtime.ensure_running()

    def test_docker_desktop_timeout_has_actionable_error(self):
        runtime = DockerContainerRuntime()
        unavailable = subprocess.CalledProcessError(1, "docker")
        with (
            patch(
                "pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run",
                side_effect=[unavailable, MagicMock(), *([unavailable] * 30)],
            ),
            patch("pynchy.plugins.runtimes.docker_runtime.runtime.sys") as system,
            patch("pynchy.plugins.runtimes.docker_runtime.runtime.time.sleep"),
        ):
            system.platform = "darwin"
            with pytest.raises(RuntimeError, match="within 60s"):
                runtime.ensure_running()

    @pytest.mark.parametrize(
        ("force", "returncode", "expected_args", "expected_result"),
        [
            (True, 0, ["docker", "rm", "-f", "old"], True),
            (False, 1, ["docker", "rm", "old"], False),
        ],
    )
    def test_remove_container_returns_cli_success(
        self, force, returncode, expected_args, expected_result
    ):
        runtime = DockerContainerRuntime()
        with patch("pynchy.plugins.runtimes.docker_runtime.runtime.subprocess.run") as run:
            run.return_value.returncode = returncode

            assert runtime.remove_container("old", force=force) is expected_result

        assert run.call_args.args[0] == expected_args


class TestAppleRuntime:
    def test_satisfies_orphan_reaping_contract(self):
        assert isinstance(AppleContainerRuntime(), OrphanReapingRuntime)

    @pytest.mark.parametrize("available", [True, False])
    def test_reports_cli_availability(self, monkeypatch, available):
        monkeypatch.setattr(
            "pynchy.plugins.runtimes.apple_runtime.runtime.shutil.which",
            lambda _cli: "container" if available else None,
        )

        assert AppleContainerRuntime().is_available() is available

    def test_ensure_running_bounds_status_probe(self):
        rt = AppleContainerRuntime()
        with patch("pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run") as mock_run:
            rt.ensure_running()

        mock_run.assert_called_once_with(
            ["container", "system", "status"],
            capture_output=True,
            check=True,
            timeout=5,
        )

    def test_status_timeout_falls_through_to_bounded_start(self):
        rt = AppleContainerRuntime()
        with patch(
            "pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run",
            side_effect=[
                subprocess.TimeoutExpired(["container", "system", "status"], timeout=5),
                MagicMock(returncode=0),
            ],
        ) as mock_run:
            rt.ensure_running()

        assert mock_run.call_args_list[1].kwargs["timeout"] == 30

    def test_start_failure_is_reported_after_status_failure(self):
        rt = AppleContainerRuntime()
        with (
            patch(
                "pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run",
                side_effect=[
                    subprocess.SubprocessError("status unavailable"),
                    OSError("container unavailable"),
                ],
            ),
            pytest.raises(RuntimeError, match="failed to start"),
        ):
            rt.ensure_running()

    def test_parses_container_status_object_format(self):
        rt = AppleContainerRuntime()
        output = json.dumps(
            [
                {
                    "configuration": {"id": "pynchy-admin-1"},
                    "status": {"state": "running"},
                },
                {
                    "configuration": {"id": "pynchy-admin-2"},
                    "status": {"state": "stopped"},
                },
                {
                    "configuration": {"id": "other-container"},
                    "status": {"state": "running"},
                },
            ]
        )
        with patch("pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = output
            result = rt.list_running_containers("pynchy-")
        assert result == ["pynchy-admin-1"]
        assert mock_run.call_args.kwargs["timeout"] == 5

    def test_list_containers_marks_agent_by_label_or_legacy_image(self):
        rt = AppleContainerRuntime()
        output = json.dumps(
            [
                {
                    "configuration": {
                        "id": "pynchy-labeled",
                        "image": {"reference": "custom:latest"},
                        "labels": {"com.pynchy.role": "agent"},
                    },
                    "status": {"state": "running"},
                },
                {
                    "configuration": {
                        "id": "pynchy-legacy",
                        "image": {"reference": "pynchy-agent:latest"},
                        "labels": {},
                    },
                    "status": {"state": "stopped"},
                },
                {
                    "configuration": {
                        "id": "pynchy-litellm",
                        "image": {"reference": "ghcr.io/berriai/litellm:main-latest"},
                        "labels": {},
                    },
                    "status": {"state": "running"},
                },
            ]
        )
        with patch("pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = output
            result = rt.list_containers("pynchy-")

        assert [item.name for item in result if item.is_agent_container] == [
            "pynchy-labeled",
            "pynchy-legacy",
        ]

    def test_list_containers_skips_nonobjects_and_invalid_creation_dates(self):
        rt = AppleContainerRuntime()
        output = json.dumps(
            [
                None,
                {"configuration": "malformed"},
                {
                    "configuration": {
                        "id": "pynchy-invalid-date",
                        "creationDate": "not-a-date",
                    },
                    "status": "running",
                },
            ]
        )
        with patch("pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = output

            result = rt.list_containers("pynchy-")

        assert len(result) == 1
        assert result[0].created_at is None

    def test_list_containers_treats_nonmapping_labels_as_empty(self):
        rt = AppleContainerRuntime()
        output = json.dumps(
            [
                {
                    "configuration": {
                        "id": "pynchy-malformed-labels",
                        "labels": "not-a-label-map",
                    }
                }
            ]
        )
        with patch("pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.stdout = output

            result = rt.list_containers()

        assert result[0].labels == {}

    def test_cleanup_builder_stops_and_removes_buildkit(self):
        rt = AppleContainerRuntime()
        with patch("pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run") as mock_run:
            rt.cleanup_builder()

        assert mock_run.call_args_list[0].args[0] == ["container", "builder", "stop"]
        assert mock_run.call_args_list[1].args[0] == [
            "container",
            "builder",
            "rm",
            "--force",
        ]
        assert all(call.kwargs["timeout"] == 15 for call in mock_run.call_args_list)

    def test_remove_container_is_bounded(self):
        rt = AppleContainerRuntime()
        with patch("pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert rt.remove_container("pynchy-stale") is True

        assert mock_run.call_args.kwargs["timeout"] == 15

    def test_remove_container_can_omit_force(self):
        rt = AppleContainerRuntime()
        with patch("pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1

            assert rt.remove_container("pynchy-stale", force=False) is False

        assert mock_run.call_args.args[0] == ["container", "rm", "pynchy-stale"]

    def test_prune_images_prunes_dangling_images(self):
        rt = AppleContainerRuntime()
        with patch("pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            assert rt.prune_images() is True

        mock_run.assert_called_once_with(
            ["container", "image", "prune"],
            capture_output=True,
            text=True,
            input="",
            timeout=300,
            check=False,
        )

    def test_prune_images_can_prune_all_unused_images(self):
        rt = AppleContainerRuntime()
        with patch("pynchy.plugins.runtimes.apple_runtime.runtime.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0

            assert rt.prune_images(all_images=True) is True

        assert mock_run.call_args.args[0] == ["container", "image", "prune", "--all"]


class TestGetRuntime:
    @pytest.fixture(autouse=True)
    def _clear_runtime_cache(self):
        runtime_mod.get_runtime.cache_clear()
        yield
        runtime_mod.get_runtime.cache_clear()

    def test_caches_result(self):
        try:
            with patch("pynchy.plugins.runtimes.detection.detect_runtime") as mock_detect:
                mock_detect.return_value = DockerContainerRuntime()
                r1 = runtime_mod.get_runtime()
                r2 = runtime_mod.get_runtime()
            assert r1 is r2
            mock_detect.assert_called_once()
        finally:
            runtime_mod.get_runtime.cache_clear()
