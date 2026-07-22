"""Tests for src/pynchy/system_checks.py.

Tests container system bootstrap logic.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.plugins.runtimes.system_checks import (
    ensure_agent_image_available,
    ensure_container_system_running,
)

# ---------------------------------------------------------------------------
# ensure_container_system_running
# ---------------------------------------------------------------------------


@dataclass
class FakeContainer:
    name: str
    state: str
    image: str = "pynchy-agent:latest"
    created_at: datetime | None = None
    is_agent_container: bool = True


class TestEnsureContainerSystemRunning:
    """Test container runtime bootstrap and orphan cleanup."""

    @pytest.fixture
    def mock_runtime(self):
        """Create a mock runtime object."""
        runtime = MagicMock()
        runtime.cli = "docker"
        runtime.list_running_containers.return_value = []
        runtime.list_containers.return_value = []
        runtime.remove_container.return_value = True
        runtime.prune_images.return_value = True
        return runtime

    @staticmethod
    def _settings(tmp_path):
        return make_settings(project_root=tmp_path)

    def test_image_exists_no_orphans(self, mock_runtime):
        """Happy path: image exists, no orphaned containers."""
        image_inspect = MagicMock(returncode=0)
        events: list[str] = []
        mock_runtime.cleanup_builder.side_effect = lambda: events.append("builder")
        mock_runtime.prune_images.side_effect = lambda **_kwargs: events.append("images") or True

        def inspect_image(*_args, **_kwargs):
            events.append("inspect")
            return image_inspect

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run",
                side_effect=inspect_image,
            ),
        ):
            ensure_container_system_running()

        mock_runtime.ensure_running.assert_called_once()
        mock_runtime.cleanup_builder.assert_called_once()
        mock_runtime.prune_images.assert_called_once_with(all_images=False)
        assert events[:3] == ["builder", "images", "inspect"]

    def test_image_missing_builds(self, mock_runtime, tmp_path):
        """On-demand image validation builds an image that is not present."""
        inspect_fail = MagicMock(returncode=1)
        requirements_ok = MagicMock(returncode=0)
        build_ok = MagicMock(returncode=0)

        # Create a fake Dockerfile
        container_dir = tmp_path / "src" / "pynchy" / "agent"
        container_dir.mkdir(parents=True)
        (container_dir / "Dockerfile").touch()

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run",
                side_effect=[inspect_fail, requirements_ok, build_ok],
            ) as run,
            patch(
                "pynchy.plugins.runtimes.system_checks.get_settings",
                return_value=self._settings(tmp_path),
            ),
        ):
            ensure_agent_image_available()

        mock_runtime.ensure_running.assert_called_once()
        assert mock_runtime.cleanup_builder.call_count == 2
        assert mock_runtime.prune_images.call_count == 2
        assert run.call_args_list[1].args[0] == [
            sys.executable,
            str(container_dir / "scripts" / "generate_plugin_requirements.py"),
            "--output",
            str(container_dir / "requirements-plugins.txt"),
            "--config",
            str(tmp_path / "config.toml"),
        ]

    def test_runtime_harness_defers_agent_image_validation(self, mock_runtime, monkeypatch):
        """Harness startup does not build an image until it actually needs an agent."""
        monkeypatch.setenv("PYNCHY_RUNTIME_HARNESS", "1")

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch("pynchy.plugins.runtimes.system_checks.subprocess.run") as run,
        ):
            ensure_container_system_running()

        mock_runtime.ensure_running.assert_called_once()
        mock_runtime.prune_images.assert_called_once_with(all_images=False)
        run.assert_not_called()

    def test_image_missing_no_dockerfile_raises(self, mock_runtime, tmp_path):
        """Image not found and no Dockerfile — should raise RuntimeError."""
        inspect_fail = MagicMock(returncode=1)

        # No Dockerfile exists
        container_dir = tmp_path / "src" / "pynchy" / "agent"
        container_dir.mkdir(parents=True)

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run", return_value=inspect_fail
            ),
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
        requirements_ok = MagicMock(returncode=0)
        build_fail = MagicMock(returncode=1)

        container_dir = tmp_path / "src" / "pynchy" / "agent"
        container_dir.mkdir(parents=True)
        (container_dir / "Dockerfile").touch()

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run",
                side_effect=[inspect_fail, requirements_ok, build_fail],
            ),
            patch(
                "pynchy.plugins.runtimes.system_checks.get_settings",
                return_value=self._settings(tmp_path),
            ),
            pytest.raises(RuntimeError, match="Failed to build"),
        ):
            ensure_container_system_running()

        mock_runtime.cleanup_builder.assert_called()
        mock_runtime.prune_images.assert_called_with(all_images=False)

    def test_missing_image_refuses_build_when_preflight_cleanup_fails(self, mock_runtime, tmp_path):
        """A missing image must not allocate more disk after cleanup failed."""
        mock_runtime.prune_images.return_value = False
        inspect_fail = MagicMock(returncode=1)

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run",
                return_value=inspect_fail,
            ) as run,
            patch(
                "pynchy.plugins.runtimes.system_checks.get_settings",
                return_value=self._settings(tmp_path),
            ),
            pytest.raises(RuntimeError, match="clean stale container build state"),
        ):
            ensure_agent_image_available()

        run.assert_called_once()

    def test_plugin_requirements_generation_failure_raises(self, mock_runtime, tmp_path):
        """A missing plugin requirements file never falls through to Docker build."""
        inspect_fail = MagicMock(returncode=1)
        requirements_fail = MagicMock(returncode=1)

        container_dir = tmp_path / "src" / "pynchy" / "agent"
        container_dir.mkdir(parents=True)
        (container_dir / "Dockerfile").touch()

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run",
                side_effect=[inspect_fail, requirements_fail],
            ) as run,
            patch(
                "pynchy.plugins.runtimes.system_checks.get_settings",
                return_value=self._settings(tmp_path),
            ),
            pytest.raises(RuntimeError, match="generate container plugin requirements"),
        ):
            ensure_agent_image_available()

        assert run.call_count == 2
        mock_runtime.cleanup_builder.assert_called_once()
        mock_runtime.prune_images.assert_called_once_with(all_images=False)

    def test_orphaned_agent_containers_reaped(self, mock_runtime):
        """Orphaned stopped agent containers should be removed."""
        mock_runtime.list_containers.return_value = [
            FakeContainer("pynchy-group-a", "stopped", created_at=datetime.now(UTC)),
            FakeContainer("pynchy-group-b", "exited", created_at=datetime.now(UTC)),
        ]
        image_inspect = MagicMock(returncode=0)

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run",
                return_value=image_inspect,
            ),
            patch(
                "pynchy.host.container_manager.session.active_session_container_names",
                return_value=set(),
            ),
        ):
            ensure_container_system_running()

        mock_runtime.remove_container.assert_any_call("pynchy-group-a", force=True)
        mock_runtime.remove_container.assert_any_call("pynchy-group-b", force=True)

    def test_orphan_reap_failure_suppressed(self, mock_runtime):
        """Errors removing orphaned agent containers should not propagate."""
        mock_runtime.list_containers.return_value = [
            FakeContainer("pynchy-stuck", "stopped", created_at=datetime.now(UTC)),
        ]
        mock_runtime.remove_container.side_effect = OSError("remove failed")

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run",
                return_value=MagicMock(returncode=0),
            ),
            patch(
                "pynchy.host.container_manager.session.active_session_container_names",
                return_value=set(),
            ),
        ):
            ensure_container_system_running()  # Should not raise
