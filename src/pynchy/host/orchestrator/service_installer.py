"""Platform service installation for auto-restart (launchd/systemd)."""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404, RUF100 - installer invokes fixed system managers with argv and no shell.
import sys
from pathlib import Path

from pynchy.config import get_settings
from pynchy.logger import logger

_LAUNCHCTL = "/bin/launchctl"
_XATTR = "/usr/bin/xattr"
_SYSTEMCTL = "/usr/bin/systemctl"
_SUDO = "/usr/bin/sudo"
_LOGINCTL = "/usr/bin/loginctl"


def is_launchd_managed() -> bool:
    """Check if this process was started by launchd (PPID 1)."""
    import os

    return os.getppid() == 1


def is_launchd_loaded(label: str) -> bool:
    """Check if a launchd job is loaded."""
    result = subprocess.run(  # noqa: S603, RUF100 - fixed absolute launchctl argv; no shell.
        [_LAUNCHCTL, "print", f"{_launchd_domain()}/{label}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _launchd_domain() -> str:
    """Return the per-user launchd GUI domain for LaunchAgents."""
    return f"gui/{os.getuid()}"


def _bootstrap_launchd_service(label: str, plist_path: Path) -> bool:
    """Bootstrap a LaunchAgent and verify launchd registered the label."""
    domain = _launchd_domain()
    result = subprocess.run(  # noqa: S603, RUF100 - fixed absolute launchctl argv; no shell.
        [_LAUNCHCTL, "bootstrap", domain, str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if is_launchd_loaded(label):
        return True

    logger.warning(
        "launchd bootstrap did not register service; resetting label state",
        label=label,
        returncode=result.returncode,
        stderr=result.stderr.strip() or None,
    )
    label_target = f"{domain}/{label}"
    subprocess.run(  # noqa: S603, RUF100 - fixed absolute launchctl argv; no shell.
        [_LAUNCHCTL, "bootout", label_target], capture_output=True, check=False
    )
    subprocess.run(  # noqa: S603, RUF100 - fixed absolute launchctl argv; no shell.
        [_LAUNCHCTL, "disable", label_target], capture_output=True, check=False
    )
    subprocess.run(  # noqa: S603, RUF100 - fixed absolute launchctl argv; no shell.
        [_LAUNCHCTL, "enable", label_target], capture_output=True, check=False
    )
    result = subprocess.run(  # noqa: S603, RUF100 - fixed absolute launchctl argv; no shell.
        [_LAUNCHCTL, "bootstrap", domain, str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if is_launchd_loaded(label):
        return True

    logger.warning(
        "launchd service still not registered after retry",
        label=label,
        returncode=result.returncode,
        stderr=result.stderr.strip() or None,
    )
    return False


def _remove_launchd_extended_attrs(path: Path) -> None:
    """Remove file metadata that can keep a LaunchAgent from bootstrapping.

    Plists copied from browsers, AirDrop, or synced folders can carry quarantine
    or provenance xattrs. launchd treats those as file policy metadata, not app
    config, so the installer strips them from the rendered service definition.
    """
    for attr in ("com.apple.quarantine", "com.apple.provenance"):
        subprocess.run(  # noqa: S603, RUF100 - fixed absolute xattr argv; no shell.
            [_XATTR, "-d", attr, str(path)], capture_output=True, check=False
        )


def _launchd_path(home: Path, uv_path: str) -> str:
    """Build a launchd PATH that can find uv and Homebrew-installed runtimes."""
    parts = [
        home / ".local" / "bin",
        Path(uv_path).parent,
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
    ]
    seen: set[str] = set()
    path_values: list[str] = []
    for path in parts:
        value = str(path)
        if value in seen:
            continue
        seen.add(value)
        path_values.append(value)
    return ":".join(path_values)


def _launchd_paths(label: str, project_root: Path) -> tuple[Path, Path]:
    src = project_root / "launchd" / f"{label}.plist"
    dest = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    return src, dest


def _render_launchd_plist(src: Path, *, project_root: Path, home: Path) -> str:
    uv_path = shutil.which("uv") or str(home / ".local" / "bin" / "uv")
    rendered = (
        src.read_text(encoding="utf-8")
        .replace("$HOME/src/PERSONAL/pynchy", str(project_root))
        .replace("$HOME/.local/bin/uv", uv_path)
        .replace("$HOME", str(home))
    )
    return rendered.replace(
        f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        _launchd_path(home, uv_path),
    )


def _launchd_file_changed(dest: Path, rendered: str) -> bool:
    return not dest.exists() or dest.read_text(encoding="utf-8") != rendered


def _write_launchd_plist(
    *,
    dest: Path,
    rendered: str,
    already_loaded: bool,
) -> None:
    if already_loaded:
        # Unload before overwriting so launchd picks up the updated version.
        subprocess.run(  # noqa: S603, RUF100 - fixed absolute launchctl argv; no shell.
            [_LAUNCHCTL, "bootout", _launchd_domain(), str(dest)],
            capture_output=True,
            check=False,
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(rendered, encoding="utf-8")
    _remove_launchd_extended_attrs(dest)
    logger.info("Installed launchd plist", dest=str(dest))


def _should_load_launchd_service(*, already_loaded: bool) -> bool:
    return already_loaded or is_launchd_managed()


def install_service() -> None:
    """Install the platform service file so the process auto-restarts on exit.

    On macOS: copies plist to ~/Library/LaunchAgents/ and loads it into
    launchd if we're already running under launchd (safe reload). When
    running manually, only copies the file to avoid spawning a competing
    second instance — the user bootstraps the LaunchAgent once to activate.

    On Linux: installs systemd user service with auto-restart.
    """
    if sys.platform == "darwin":
        _install_launchd_service()
    elif sys.platform == "linux":
        _install_systemd_service()


def _install_launchd_service() -> None:
    """Install macOS launchd service."""
    label = "com.pynchy"
    project_root = get_settings().project_root
    src, dest = _launchd_paths(label, project_root)
    if not src.exists():
        logger.warning("launchd plist not found in repo, skipping service install")
        return
    home = Path.home()
    # launchd does not expand $HOME (or any env var) inside plist strings, so
    # the template's placeholders must be substituted before writing.
    rendered = _render_launchd_plist(src, project_root=project_root, home=home)
    already_loaded = is_launchd_loaded(label)
    file_changed = _launchd_file_changed(dest, rendered)
    if not file_changed and already_loaded:
        return  # already up to date and loaded
    if file_changed:
        _write_launchd_plist(dest=dest, rendered=rendered, already_loaded=already_loaded)
    # Only load if we're already running under launchd (safe to reload).
    # When running manually, loading would spawn a competing instance
    # that fights over channel websockets and port binding.
    if _should_load_launchd_service(already_loaded=already_loaded):
        if _bootstrap_launchd_service(label, dest):
            logger.info("Loaded launchd service", label=label)
    elif not already_loaded:
        logger.info(
            "Launchd plist installed. To enable auto-restart, stop this "
            "process and run: launchctl bootstrap gui/$(id -u) "
            "~/Library/LaunchAgents/com.pynchy.plist"
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
    if dest.exists() and dest.read_text(encoding="utf-8") == unit:
        return  # already up to date
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(unit, encoding="utf-8")
    logger.info("Installed systemd user service", dest=str(dest))
    subprocess.run(  # noqa: S603, RUF100 - fixed absolute systemctl argv; no shell.
        [_SYSTEMCTL, "--user", "daemon-reload"],
        capture_output=True,
        check=False,
    )
    subprocess.run(  # noqa: S603, RUF100 - fixed absolute systemctl argv; no shell.
        [_SYSTEMCTL, "--user", "enable", "pynchy.service"],
        capture_output=True,
        check=False,
    )
    # Enable lingering so the user service runs without an active login session
    subprocess.run(  # noqa: S603, RUF100 - fixed absolute sudo argv; no shell.
        [_SUDO, _LOGINCTL, "enable-linger", home.name],
        capture_output=True,
        check=False,
    )
