"""Tests for src/pynchy/system_checks.py.

Tests container system bootstrap logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.plugins.runtimes.system_checks import ensure_container_system_running

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
        return runtime

    @staticmethod
    def _settings(tmp_path):
        return make_settings(project_root=tmp_path)

    def test_image_exists_no_orphans(self, mock_runtime):
        """Happy path: image exists, no orphaned containers."""
        image_inspect = MagicMock(returncode=0)

        with (
            patch("pynchy.plugins.runtimes.system_checks.get_runtime", return_value=mock_runtime),
            patch(
                "pynchy.plugins.runtimes.system_checks.subprocess.run", return_value=image_inspect
            ),
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

        mock_runtime.cleanup_builder.assert_called_once()

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
