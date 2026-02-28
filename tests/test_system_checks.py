"""Tests for src/pynchy/system_checks.py.

Tests container system bootstrap logic.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.plugins.runtimes.system_checks import ensure_container_system_running

# ---------------------------------------------------------------------------
# ensure_container_system_running
# ---------------------------------------------------------------------------


class TestEnsureContainerSystemRunning:
    """Test container runtime bootstrap and orphan cleanup."""

    @pytest.fixture
    def mock_runtime(self):
        """Create a mock runtime object."""
        runtime = MagicMock()
        runtime.cli = "docker"
        runtime.list_running_containers.return_value = []
        return runtime

    @staticmethod
    def _settings(tmp_path):
        return make_settings(project_root=tmp_path)

    def test_image_exists_no_orphans(self, mock_runtime):
        """Happy path: image exists, no orphaned containers."""
        image_inspect = MagicMock(returncode=0)

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch("pynchy.plugins.runtimes.system_checks.subprocess.run", return_value=image_inspect),
        ):
            ensure_container_system_running()

        mock_runtime.ensure_running.assert_called_once()

    def test_image_missing_builds(self, mock_runtime, tmp_path):
        """Image not found — should trigger build."""
        inspect_fail = MagicMock(returncode=1)
        build_ok = MagicMock(returncode=0)

        # Create a fake Dockerfile
        container_dir = tmp_path / "src" / "pynchy" / "agent"
        container_dir.mkdir(parents=True)
        (container_dir / "Dockerfile").touch()

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run",
                side_effect=[inspect_fail, build_ok],
            ),
            patch(
                "pynchy.plugins.runtimes.system_checks.get_settings",
                return_value=self._settings(tmp_path),
            ),
        ):
            ensure_container_system_running()

    def test_image_missing_no_dockerfile_raises(self, mock_runtime, tmp_path):
        """Image not found and no Dockerfile — should raise RuntimeError."""
        inspect_fail = MagicMock(returncode=1)

        # No Dockerfile exists
        container_dir = tmp_path / "src" / "pynchy" / "agent"
        container_dir.mkdir(parents=True)

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch("pynchy.plugins.runtimes.system_checks.subprocess.run", return_value=inspect_fail),
            patch(
                "pynchy.plugins.runtimes.system_checks.get_settings",
                return_value=self._settings(tmp_path),
            ),
            pytest.raises(RuntimeError, match="not found"),
        ):
            ensure_container_system_running()

    def test_build_failure_raises(self, mock_runtime, tmp_path):
        """Image build fails — should raise RuntimeError."""
        inspect_fail = MagicMock(returncode=1)
        build_fail = MagicMock(returncode=1)

        container_dir = tmp_path / "src" / "pynchy" / "agent"
        container_dir.mkdir(parents=True)
        (container_dir / "Dockerfile").touch()

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run",
                side_effect=[inspect_fail, build_fail],
            ),
            patch(
                "pynchy.plugins.runtimes.system_checks.get_settings",
                return_value=self._settings(tmp_path),
            ),
            pytest.raises(RuntimeError, match="Failed to build"),
        ):
            ensure_container_system_running()

    def test_orphaned_containers_stopped(self, mock_runtime):
        """Orphaned pynchy containers should be stopped."""
        mock_runtime.list_running_containers.return_value = [
            "pynchy-group-a",
            "pynchy-group-b",
        ]
        image_inspect = MagicMock(returncode=0)

        stop_calls = []

        def track_run(cmd, **kwargs):
            if cmd[0] == "docker" and "stop" in cmd:
                stop_calls.append(cmd[-1])
                return MagicMock(returncode=0)
            return image_inspect

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch("pynchy.plugins.runtimes.system_checks.subprocess.run", side_effect=track_run),
        ):
            ensure_container_system_running()

        assert "pynchy-group-a" in stop_calls
        assert "pynchy-group-b" in stop_calls

    def test_orphan_stop_failure_suppressed(self, mock_runtime):
        """Errors stopping orphans should not propagate."""
        mock_runtime.list_running_containers.return_value = ["pynchy-stuck"]

        call_count = [0]

        def track_run(cmd, **kwargs):
            call_count[0] += 1
            if "stop" in cmd:
                raise subprocess.SubprocessError("stop failed")
            return MagicMock(returncode=0)

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch("pynchy.plugins.runtimes.system_checks.subprocess.run", side_effect=track_run),
        ):
            ensure_container_system_running()  # Should not raise
