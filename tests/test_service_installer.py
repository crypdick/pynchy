"""Tests for platform service installation (launchd/systemd).

Tests critical business logic:
- is_launchd_managed() detection
- is_launchd_loaded() subprocess check
- install_service() file diffing and safe launchd activation logic
- install_service() unit file generation and idempotency
- install_service() platform dispatch
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from pynchy.host.orchestrator.service_installer import (
    install_service,
    is_launchd_loaded,
    is_launchd_managed,
)

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
        with patch("os.getuid", return_value=501), patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)
            assert is_launchd_loaded("com.pynchy") is True
            mock_run.assert_called_once_with(
                ["/bin/launchctl", "print", "gui/501/com.pynchy"],
                capture_output=True,
                check=False,
            )

    def test_returns_false_when_job_is_not_loaded(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1)
            assert is_launchd_loaded("com.pynchy") is False


class TestInstallService:
    """Test platform-based dispatch."""

    def test_dispatches_to_launchd_on_darwin(self, tmp_path: Path):
        with patch("pynchy.host.orchestrator.service_installer.sys") as mock_sys:
            mock_sys.platform = "darwin"
            with patch(
                "pynchy.host.orchestrator.service_installer._install_launchd_service"
            ) as mock_launchd:
                install_service(tmp_path)
                mock_launchd.assert_called_once_with(tmp_path)

    def test_ephemeral_runtime_skips_installation(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("PYNCHY_DISABLE_SERVICE_INSTALL", "1")
        with (
            patch("pynchy.host.orchestrator.service_installer._install_launchd_service") as launchd,
            patch("pynchy.host.orchestrator.service_installer._install_systemd_service") as systemd,
        ):
            install_service(tmp_path)
        launchd.assert_not_called()
        systemd.assert_not_called()

    def test_dispatches_to_systemd_on_linux(self, tmp_path: Path):
        with patch("pynchy.host.orchestrator.service_installer.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch(
                "pynchy.host.orchestrator.service_installer._install_systemd_service"
            ) as mock_systemd:
                install_service(tmp_path)
                mock_systemd.assert_called_once_with(tmp_path)

    def test_does_nothing_on_unsupported_platform(self, tmp_path: Path):
        with patch("pynchy.host.orchestrator.service_installer.sys") as mock_sys:
            mock_sys.platform = "win32"
            with (
                patch(
                    "pynchy.host.orchestrator.service_installer._install_launchd_service"
                ) as mock_launchd,
                patch(
                    "pynchy.host.orchestrator.service_installer._install_systemd_service"
                ) as mock_systemd,
            ):
                install_service(tmp_path)
                mock_launchd.assert_not_called()
                mock_systemd.assert_not_called()


# ---------------------------------------------------------------------------
# _install_launchd_service
# ---------------------------------------------------------------------------


class TestInstallLaunchdService:
    """Test macOS launchd service installation logic.

    Drives the public ``install_service()`` entry point with the platform
    forced to darwin so dispatch routes to the launchd installer.
    """

    @pytest.fixture(autouse=True)
    def _force_darwin(self):
        with patch("pynchy.host.orchestrator.service_installer.sys.platform", "darwin"):
            yield

    def test_skips_when_plist_source_does_not_exist(self, tmp_path: Path):
        """Should log warning and return when source plist is missing."""
        # Source file does not exist.
        install_service(tmp_path)

    def test_copies_plist_when_dest_does_not_exist(self, tmp_path: Path):
        """Should copy plist and log when destination doesn't exist."""
        # Create source plist
        src_dir = tmp_path / "launchd"
        src_dir.mkdir()
        plist_content = "<plist>test</plist>"
        (src_dir / "com.pynchy.plist").write_text(plist_content)

        dest_dir = tmp_path / "Library" / "LaunchAgents"

        with (
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_loaded", return_value=False
            ),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_managed", return_value=False
            ),
            patch("subprocess.run") as mock_run,
        ):
            install_service(tmp_path)

        dest_file = dest_dir / "com.pynchy.plist"
        assert dest_file.exists()
        assert dest_file.read_text() == plist_content
        # Should NOT have bootstrapped launchd (not managed, not previously loaded).
        bootstrap_calls = [
            c for c in mock_run.call_args_list if c.args[0][:2] == ["/bin/launchctl", "bootstrap"]
        ]
        assert not bootstrap_calls

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
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_loaded", return_value=True
            ),
            patch("subprocess.run") as mock_run,
        ):
            install_service(tmp_path)

        # No subprocess calls because nothing changed
        mock_run.assert_not_called()

    def test_changed_plist_cannot_unregister_its_running_launchd_job(self, tmp_path: Path):
        """A loaded service must leave activation to an external process."""
        src_dir = tmp_path / "launchd"
        src_dir.mkdir()
        (src_dir / "com.pynchy.plist").write_text("<plist>new</plist>")

        dest_dir = tmp_path / "Library" / "LaunchAgents"
        dest_dir.mkdir(parents=True)
        (dest_dir / "com.pynchy.plist").write_text("<plist>old</plist>")

        with (
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_loaded", return_value=True
            ),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_managed", return_value=False
            ),
            patch("subprocess.run") as mock_run,
        ):
            install_service(tmp_path)

        service_commands = [
            call.args[0]
            for call in mock_run.call_args_list
            if call.args[0][:2]
            in (
                ["/bin/launchctl", "bootout"],
                ["/bin/launchctl", "bootstrap"],
            )
        ]
        assert service_commands == []

        # File should be updated
        assert (dest_dir / "com.pynchy.plist").read_text() == "<plist>new</plist>"

    def test_substitutes_home_and_project_root_placeholders(self, tmp_path: Path):
        """launchd does not expand $HOME in plist strings, so the installer
        must substitute it itself before writing the file."""
        src_dir = tmp_path / "launchd"
        src_dir.mkdir()
        (src_dir / "com.pynchy.plist").write_text(
            "<string>$HOME/.local/bin/uv</string>\n"
            "<string>$PYNCHY_PROJECT_ROOT</string>\n"
            "<string>$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin</string>\n"
            "<string>$HOME</string>\n"
        )

        with (
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch("shutil.which", return_value="/opt/homebrew/bin/uv"),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_loaded", return_value=False
            ),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_managed", return_value=False
            ),
            patch("subprocess.run"),
        ):
            install_service(tmp_path)

        content = (tmp_path / "Library" / "LaunchAgents" / "com.pynchy.plist").read_text()
        assert "$HOME" not in content
        assert "/opt/homebrew/bin/uv" in content
        assert "/opt/homebrew/bin:" in content
        assert str(tmp_path) in content

    def test_repository_launchd_template_uses_host_secret_launcher(self, tmp_path: Path) -> None:
        project_root = Path(__file__).resolve().parents[1]
        source = project_root / "launchd" / "com.pynchy.plist"
        destination = tmp_path / "Library" / "LaunchAgents" / "com.pynchy.plist"

        with (
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_loaded",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_managed",
                return_value=False,
            ),
            patch("subprocess.run"),
        ):
            install_service(project_root)

        content = destination.read_text(encoding="utf-8")
        assert str(project_root / "scripts" / "run_pynchy.sh") in content
        assert source.exists()

    def test_loads_when_running_under_launchd(self, tmp_path: Path):
        """Should load the service when the process is managed by launchd."""
        src_dir = tmp_path / "launchd"
        src_dir.mkdir()
        (src_dir / "com.pynchy.plist").write_text("<plist>test</plist>")

        with (
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_loaded",
                side_effect=[False, True],
            ),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_managed", return_value=True
            ),
            patch("subprocess.run") as mock_run,
        ):
            install_service(tmp_path)

        bootstrap_calls = [
            c for c in mock_run.call_args_list if c.args[0][:2] == ["/bin/launchctl", "bootstrap"]
        ]
        assert len(bootstrap_calls) == 1

    def test_recovers_when_bootstrap_does_not_register_launchd_label(self, tmp_path: Path):
        """launchd can keep stale disabled state for a label after bootout.

        In that case bootstrap may return success while ``launchctl print`` still
        cannot find the job. The installer should reset the disabled-state entry
        and retry so reboot persistence is not left half-installed.
        """
        src_dir = tmp_path / "launchd"
        src_dir.mkdir()
        (src_dir / "com.pynchy.plist").write_text("<plist>test</plist>")

        loaded_states = iter([False, False, True])

        with (
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch("pynchy.host.orchestrator.service_installer.os.getuid", return_value=501),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_loaded",
                side_effect=lambda _label: next(loaded_states),
            ),
            patch(
                "pynchy.host.orchestrator.service_installer.is_launchd_managed", return_value=True
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            install_service(tmp_path)

        cmds = [c.args[0] for c in mock_run.call_args_list]
        bootstrap_cmds = [c for c in cmds if c[:2] == ["/bin/launchctl", "bootstrap"]]
        assert len(bootstrap_cmds) == 2
        assert ["/bin/launchctl", "disable", "gui/501/com.pynchy"] in cmds
        assert ["/bin/launchctl", "enable", "gui/501/com.pynchy"] in cmds


# ---------------------------------------------------------------------------
# _install_systemd_service
# ---------------------------------------------------------------------------


class TestInstallSystemdService:
    """Test Linux systemd service installation logic.

    Drives the public ``install_service()`` entry point with the platform
    forced to linux so dispatch routes to the systemd installer.
    """

    @pytest.fixture(autouse=True)
    def _force_linux(self):
        with patch("pynchy.host.orchestrator.service_installer.sys.platform", "linux"):
            yield

    def test_skips_when_uv_not_found(self, tmp_path: Path):
        """Should warn and return when uv is not in PATH."""
        with patch("shutil.which", return_value=None):
            # Should not raise
            install_service(tmp_path)

    def test_creates_service_file(self, tmp_path: Path):
        """Should create systemd unit file with correct content."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch("subprocess.run"),
        ):
            install_service(tmp_path)

        unit_file = tmp_path / ".config" / "systemd" / "user" / "pynchy.service"
        assert unit_file.exists()
        content = unit_file.read_text()
        assert "Description=Pynchy personal assistant" in content
        assert f"WorkingDirectory={tmp_path}" in content
        assert "ExecStartPre=/usr/local/bin/uv tool run prek install" in content
        assert "uv sync" not in content
        assert f"ExecStart=/bin/sh {tmp_path}/scripts/run_pynchy.sh" in content
        assert "Restart=always" in content
        assert "RestartSec=10" in content

    def test_runs_systemd_commands_after_install(self, tmp_path: Path):
        """Should reload daemon, enable service, and enable lingering."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            install_service(tmp_path)

        # Should have run daemon-reload, enable, and enable-linger
        cmd_strs = [" ".join(c.args[0]) for c in mock_run.call_args_list]
        assert any("daemon-reload" in cmd for cmd in cmd_strs)
        assert any("enable" in cmd and "pynchy.service" in cmd for cmd in cmd_strs)
        assert any("enable-linger" in cmd for cmd in cmd_strs)

    def test_skips_when_unit_file_unchanged(self, tmp_path: Path):
        """Should return early when unit file content matches."""
        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            # First install creates the file
            install_service(tmp_path)

            # Second install should detect no change and skip
            mock_run.reset_mock()
            install_service(tmp_path)
            assert mock_run.call_count == 0

    def test_overwrites_outdated_unit_file(self, tmp_path: Path):
        """Should overwrite unit file when content differs."""
        unit_dir = tmp_path / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "pynchy.service").write_text("[Unit]\nold content")

        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch("pynchy.host.orchestrator.service_installer.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            install_service(tmp_path)

        content = (unit_dir / "pynchy.service").read_text()
        assert "Description=Pynchy personal assistant" in content
        assert mock_run.call_count == 3  # daemon-reload, enable, enable-linger


def test_host_launcher_uses_an_isolated_locked_environment() -> None:
    content = Path("scripts/run_pynchy.sh").read_text(encoding="utf-8")

    assert 'UV_PROJECT_ENVIRONMENT="$project_root/data/host-venv"' in content
    assert content.count("uv run --locked --no-dev --all-extras pynchy") == 2
