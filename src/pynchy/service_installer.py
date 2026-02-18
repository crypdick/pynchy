"""Platform service installation for auto-restart (launchd/systemd)."""

from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

from pynchy.config import get_settings
from pynchy.logger import logger


def is_launchd_managed() -> bool:
    """Check if this process was started by launchd (PPID 1)."""
    import os

    return os.getppid() == 1


def is_launchd_loaded(label: str) -> bool:
    """Check if a launchd job is loaded."""
    result = subprocess.run(["launchctl", "list", label], capture_output=True)
    return result.returncode == 0


def install_service() -> None:
    """Install the platform service file so the process auto-restarts on exit.

    On macOS: copies plist to ~/Library/LaunchAgents/ and loads it into
    launchd if we're already running under launchd (safe reload). When
    running manually, only copies the file to avoid spawning a competing
    second instance — the user runs launchctl load once to activate.

    On Linux: installs systemd user service with auto-restart.
    """
    if sys.platform == "darwin":
        _install_launchd_service()
    elif sys.platform == "linux":
        _install_systemd_service()


def _install_launchd_service() -> None:
    """Install macOS launchd service."""
    label = "com.pynchy"
    src = get_settings().project_root / "launchd" / f"{label}.plist"
    dest = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if not src.exists():
        logger.warning("launchd plist not found in repo, skipping service install")
        return
    already_loaded = is_launchd_loaded(label)
    file_changed = not dest.exists() or not filecmp.cmp(str(src), str(dest), shallow=False)
    if not file_changed and already_loaded:
        return  # already up to date and loaded
    if file_changed:
        # Unload before overwriting so launchd picks up the new version
        if already_loaded:
            subprocess.run(["launchctl", "unload", str(dest)], capture_output=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        logger.info("Installed launchd plist", dest=str(dest))
    # Only load if we're already running under launchd (safe to reload).
    # When running manually, loading would spawn a competing instance
    # that fights over channel websockets and port binding.
    if already_loaded or is_launchd_managed():
        subprocess.run(["launchctl", "load", str(dest)], capture_output=True)
        logger.info("Loaded launchd service", label=label)
    elif not already_loaded:
        logger.info(
            "Launchd plist installed. To enable auto-restart, stop this "
            "process and run: launchctl load ~/Library/LaunchAgents/com.pynchy.plist"
        )


def _install_systemd_service() -> None:
    """Install Linux systemd user service."""
    uv_path = shutil.which("uv")
    if not uv_path:
        logger.warning("uv not found in PATH, skipping systemd service install")
        return
    home = Path.home()
    # TODO: Uninstall cleanup — need a way to systemctl --user disable + rm
    # this service when the user wants to remove pynchy.
    project_root = get_settings().project_root
    git_path = shutil.which("git") or "/usr/bin/git"
    unit = f"""\
[Unit]
Description=Pynchy personal assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={project_root}
ExecStartPre={git_path} -C {project_root} pull --ff-only
ExecStartPre={uv_path} sync --all-extras
ExecStartPre={uv_path} tool run pre-commit install
ExecStart={uv_path} run pynchy
Restart=always
RestartSec=10
Environment=HOME={home}
Environment=PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
"""
    dest = home / ".config" / "systemd" / "user" / "pynchy.service"
    if dest.exists() and dest.read_text() == unit:
        return  # already up to date
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(unit)
    logger.info("Installed systemd user service", dest=str(dest))
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
    )
    subprocess.run(
        ["systemctl", "--user", "enable", "pynchy.service"],
        capture_output=True,
    )
    # Enable lingering so the user service runs without an active login session
    subprocess.run(
        ["sudo", "loginctl", "enable-linger", home.name],
        capture_output=True,
    )
