"""Tests for platform service installation (launchd/systemd).

Tests critical business logic:
- is_launchd_managed() detection
- is_launchd_loaded() subprocess check
- _install_launchd_service() file diffing, unload/copy/load logic
- _install_systemd_service() unit file generation and idempotency
- install_service() platform dispatch
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

from conftest import make_settings

from pynchy.service_installer import (
    _install_launchd_service,
    _install_systemd_service,
    install_service,
    is_launchd_loaded,
    is_launchd_managed,
)


def _test_settings(*, project_root: Path):
    return make_settings(project_root=project_root)


# ---------------------------------------------------------------------------
# is_launchd_managed
# ---------------------------------------------------------------------------


class TestIsLaunchdManaged:
    """Test launchd parent process detection."""

    def test_returns_true_when_ppid_is_1(self):
        with patch("os.getppid", return_value=1):
            assert is_launchd_managed() is True

    def test_returns_false_when_ppid_is_not_1(self):
        with patch("os.getppid", return_value=12345):
            assert is_launchd_managed() is False


# ---------------------------------------------------------------------------
# is_launchd_loaded
# ---------------------------------------------------------------------------


class TestIsLaunchdLoaded:
    """Test launchd job status check."""

    def test_returns_true_when_job_is_loaded(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            assert is_launchd_loaded("com.pynchy") is True
            mock_run.assert_called_once_with(
                ["launchctl", "list", "com.pynchy"], capture_output=True
            )

    def test_returns_false_when_job_is_not_loaded(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1)
            assert is_launchd_loaded("com.pynchy") is False


# ---------------------------------------------------------------------------
# install_service (dispatch)
# ---------------------------------------------------------------------------


class TestInstallService:
    """Test platform-based dispatch."""

    def test_dispatches_to_launchd_on_darwin(self):
        with patch("pynchy.service_installer.sys") as mock_sys:
            mock_sys.platform = "darwin"
            with patch("pynchy.service_installer._install_launchd_service") as mock_launchd:
                install_service()
                mock_launchd.assert_called_once()

    def test_dispatches_to_systemd_on_linux(self):
        with patch("pynchy.service_installer.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch("pynchy.service_installer._install_systemd_service") as mock_systemd:
                install_service()
                mock_systemd.assert_called_once()

    def test_does_nothing_on_unsupported_platform(self):
        with patch("pynchy.service_installer.sys") as mock_sys:
            mock_sys.platform = "win32"
            with (
                patch("pynchy.service_installer._install_launchd_service") as mock_launchd,
                patch("pynchy.service_installer._install_systemd_service") as mock_systemd,
            ):
                install_service()
                mock_launchd.assert_not_called()
                mock_systemd.assert_not_called()


# ---------------------------------------------------------------------------
# _install_launchd_service
# ---------------------------------------------------------------------------


class TestInstallLaunchdService:
    """Test macOS launchd service installation logic."""

    def test_skips_when_plist_source_does_not_exist(self, tmp_path: Path):
        """Should log warning and return when source plist is missing."""
        with patch(
            "pynchy.service_installer.get_settings",
            return_value=_test_settings(project_root=tmp_path),
        ):
            # Source file does not exist
            _install_launchd_service()
            # No error, just skipped

    def test_copies_plist_when_dest_does_not_exist(self, tmp_path: Path):
        """Should copy plist and log when destination doesn't exist."""
        # Create source plist
        src_dir = tmp_path / "launchd"
        src_dir.mkdir()
        plist_content = "<plist>test</plist>"
        (src_dir / "com.pynchy.plist").write_text(plist_content)

        dest_dir = tmp_path / "Library" / "LaunchAgents"

        with (
            patch(
                "pynchy.service_installer.get_settings",
                return_value=_test_settings(project_root=tmp_path),
            ),
            patch("pynchy.service_installer.Path.home", return_value=tmp_path),
            patch("pynchy.service_installer.is_launchd_loaded", return_value=False),
            patch("pynchy.service_installer.is_launchd_managed", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            _install_launchd_service()

        dest_file = dest_dir / "com.pynchy.plist"
        assert dest_file.exists()
        assert dest_file.read_text() == plist_content
        # Should NOT have called launchctl load (not managed, not previously loaded)
        load_calls = [c for c in mock_run.call_args_list if "load" in str(c)]
        assert not load_calls

    def test_skips_when_file_unchanged_and_already_loaded(self, tmp_path: Path):
        """Should do nothing when plist is identical and already loaded."""
        src_dir = tmp_path / "launchd"
        src_dir.mkdir()
        plist_content = "<plist>same</plist>"
        (src_dir / "com.pynchy.plist").write_text(plist_content)

        dest_dir = tmp_path / "Library" / "LaunchAgents"
        dest_dir.mkdir(parents=True)
        (dest_dir / "com.pynchy.plist").write_text(plist_content)

        with (
            patch(
                "pynchy.service_installer.get_settings",
                return_value=_test_settings(project_root=tmp_path),
            ),
            patch("pynchy.service_installer.Path.home", return_value=tmp_path),
            patch("pynchy.service_installer.is_launchd_loaded", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            _install_launchd_service()

        # No subprocess calls because nothing changed
        mock_run.assert_not_called()

    def test_unloads_before_overwriting_when_already_loaded(self, tmp_path: Path):
        """Should unload, copy, and reload when file changed and was loaded."""
        src_dir = tmp_path / "launchd"
        src_dir.mkdir()
        (src_dir / "com.pynchy.plist").write_text("<plist>new</plist>")

        dest_dir = tmp_path / "Library" / "LaunchAgents"
        dest_dir.mkdir(parents=True)
        (dest_dir / "com.pynchy.plist").write_text("<plist>old</plist>")

        with (
            patch(
                "pynchy.service_installer.get_settings",
                return_value=_test_settings(project_root=tmp_path),
            ),
            patch("pynchy.service_installer.Path.home", return_value=tmp_path),
            patch("pynchy.service_installer.is_launchd_loaded", return_value=True),
            patch("pynchy.service_installer.is_launchd_managed", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            _install_launchd_service()

        # Should have unloaded, then loaded
        calls = mock_run.call_args_list
        cmds = [c.args[0] for c in calls]
        unload_cmds = [c for c in cmds if "unload" in c]
        load_cmds = [c for c in cmds if c[1] == "load"]
        assert len(unload_cmds) == 1
        assert len(load_cmds) == 1

        # File should be updated
        assert (dest_dir / "com.pynchy.plist").read_text() == "<plist>new</plist>"

    def test_loads_when_running_under_launchd(self, tmp_path: Path):
        """Should load the service when the process is managed by launchd."""
        src_dir = tmp_path / "launchd"
        src_dir.mkdir()
        (src_dir / "com.pynchy.plist").write_text("<plist>test</plist>")

        with (
            patch(
                "pynchy.service_installer.get_settings",
                return_value=_test_settings(project_root=tmp_path),
            ),
            patch("pynchy.service_installer.Path.home", return_value=tmp_path),
            patch("pynchy.service_installer.is_launchd_loaded", return_value=False),
            patch("pynchy.service_installer.is_launchd_managed", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            _install_launchd_service()

        load_calls = [c for c in mock_run.call_args_list if "load" in str(c)]
        assert len(load_calls) == 1


# ---------------------------------------------------------------------------
# _install_systemd_service
# ---------------------------------------------------------------------------


class TestInstallSystemdService:
    """Test Linux systemd service installation logic."""

    def test_skips_when_uv_not_found(self):
        """Should warn and return when uv is not in PATH."""
        with patch("shutil.which", return_value=None):
            # Should not raise
            _install_systemd_service()

    def test_creates_service_file(self, tmp_path: Path):
        """Should create systemd unit file with correct content."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch(
                "pynchy.service_installer.get_settings",
                return_value=_test_settings(project_root=tmp_path),
            ),
            patch("pynchy.service_installer.Path.home", return_value=tmp_path),
            patch("subprocess.run"),
        ):
            _install_systemd_service()

        unit_file = tmp_path / ".config" / "systemd" / "user" / "pynchy.service"
        assert unit_file.exists()
        content = unit_file.read_text()
        assert "Description=Pynchy personal assistant" in content
        assert f"WorkingDirectory={tmp_path}" in content
        assert "ExecStart=/usr/local/bin/uv run pynchy" in content
        assert "Restart=always" in content
        assert "RestartSec=10" in content

    def test_runs_systemd_commands_after_install(self, tmp_path: Path):
        """Should reload daemon, enable service, and enable lingering."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch(
                "pynchy.service_installer.get_settings",
                return_value=_test_settings(project_root=tmp_path),
            ),
            patch("pynchy.service_installer.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            _install_systemd_service()

        # Should have run daemon-reload, enable, and enable-linger
        cmd_strs = [" ".join(c.args[0]) for c in mock_run.call_args_list]
        assert any("daemon-reload" in cmd for cmd in cmd_strs)
        assert any("enable" in cmd and "pynchy.service" in cmd for cmd in cmd_strs)
        assert any("enable-linger" in cmd for cmd in cmd_strs)

    def test_skips_when_unit_file_unchanged(self, tmp_path: Path):
        """Should return early when unit file content matches."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch(
                "pynchy.service_installer.get_settings",
                return_value=_test_settings(project_root=tmp_path),
            ),
            patch("pynchy.service_installer.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            # First install creates the file
            _install_systemd_service()

            # Second install should detect no change and skip
            mock_run.reset_mock()
            _install_systemd_service()
            assert mock_run.call_count == 0

    def test_overwrites_outdated_unit_file(self, tmp_path: Path):
        """Should overwrite unit file when content differs."""
        unit_dir = tmp_path / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "pynchy.service").write_text("[Unit]\nold content")

        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch(
                "pynchy.service_installer.get_settings",
                return_value=_test_settings(project_root=tmp_path),
            ),
            patch("pynchy.service_installer.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            _install_systemd_service()

        content = (unit_dir / "pynchy.service").read_text()
        assert "Description=Pynchy personal assistant" in content
        assert mock_run.call_count == 3  # daemon-reload, enable, enable-linger
