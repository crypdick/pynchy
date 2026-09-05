"""Shared Playwright browser utilities for integration plugins.

Extracted from the Slack token extractor and X integration scripts to
eliminate duplication.  These are plain functions — plugin-specific logic
(anti-detection args, persistent Xvfb) stays in the respective plugin modules.

.. warning:: HARD POLICY — SYSTEM CHROME ONLY

   **NEVER use Playwright's vendored/bundled Chromium.**  Every plugin that
   launches a browser MUST pass ``executable_path=chrome_path()`` to
   ``launch_persistent_context()``.  Omitting ``executable_path`` silently
   falls back to Playwright's Chromium, which:

   1. Has a distinct browser fingerprint that services detect and block.
   2. Requires ``playwright install chromium`` (200+ MB) on every host.
   3. Produces inconsistent behavior vs. the system browser.

   This policy applies uniformly to ALL plugins — Google, Slack, X, or
   anything else — with no per-service exceptions.  Use ``chrome_path()``
   from this module; it auto-detects the system binary and raises a clear
   error if Chrome/Chromium isn't installed.

Chrome is auto-detected in standard locations; ``CHROME_PATH`` env var
can override if the binary is elsewhere.  Playwright is used only for its
automation protocol (CDP), never for its bundled browser.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess  # noqa: S404 - fixed argv process helpers; never uses shell=True.
import sys
import time
from pathlib import Path

from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Project root
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """Pynchy project root — ``PYNCHY_PROJECT_ROOT`` env var or cwd."""
    root = os.environ.get("PYNCHY_PROJECT_ROOT", "")
    return Path(root) if root else Path.cwd()


# ---------------------------------------------------------------------------
# System Chrome (never use Playwright's vendored browser)
# ---------------------------------------------------------------------------

# Well-known Chrome/Chromium binary locations per platform.  Checked in order;
# the first existing file wins.  Google Chrome is preferred over Chromium
# because its fingerprint is more common in the wild.
_CHROME_CANDIDATES_LINUX = [
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
]

_CHROME_CANDIDATES_MACOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

_INSTALL_INSTRUCTIONS = {
    "linux": (
        "Install Google Chrome:\n"
        "  wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb\n"
        "  sudo dpkg -i google-chrome-stable_current_amd64.deb\n"
        "  sudo apt-get install -f\n"
        "\n"
        "Or install Chromium:\n"
        "  sudo apt install chromium-browser"
    ),
    "darwin": (
        "Install Google Chrome:\n"
        "  brew install --cask google-chrome\n"
        "\n"
        "Or download from https://www.google.com/chrome/"
    ),
}

_CHROME_PATH_MISSING_MESSAGE = (
    "CHROME_PATH={path!r} does not exist. Install Chrome/Chromium and update CHROME_PATH in .env."
)
_CHROME_NOT_INSTALLED_MESSAGE = (
    "Chrome/Chromium is not installed (or not in a standard location).\n"
    "\n"
    "{instructions}\n"
    "\n"
    "After installing, either ensure the binary is in a standard path or "
    "set CHROME_PATH in .env to point to it."
)
_HEADLESS_DISPLAY_REQUIRED_MESSAGE = (
    "Headless display requires: {error}. Install with: apt install xvfb x11vnc novnc"
)


def _detect_chrome() -> str | None:
    """Auto-detect Chrome/Chromium in well-known locations.

    Returns the path to the first found binary, or None.
    """
    if sys.platform == "darwin":
        candidates = _CHROME_CANDIDATES_MACOS
    else:
        candidates = _CHROME_CANDIDATES_LINUX

    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate

    # Fall back to PATH lookup (handles unusual installs / WSL / Nix / etc.)
    for name in ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium"):
        found = shutil.which(name)
        if found:
            return found

    return None


def chrome_path() -> str:
    """Return the system Chrome/Chromium binary path.

    Resolution order:
    1. ``CHROME_PATH`` environment variable (explicit override)
    2. Auto-detection in well-known locations per platform
    3. ``RuntimeError`` with platform-specific install instructions

    All browser plugins must use the system browser.  Playwright's vendored
    Chromium has a distinct fingerprint that services (notably X) detect and
    block as bot traffic.  Playwright is used only for its CDP automation
    protocol, never for its bundled browser binary.
    """
    # 1. Explicit override via env var
    path = os.environ.get("CHROME_PATH", "")
    if path:
        if not Path(path).is_file():
            raise RuntimeError(_CHROME_PATH_MISSING_MESSAGE.format(path=path))
        return path

    # 2. Auto-detect
    detected = _detect_chrome()
    if detected:
        return detected

    # 3. Not found — give platform-specific install instructions
    platform_key = "darwin" if sys.platform == "darwin" else "linux"
    instructions = _INSTALL_INSTRUCTIONS[platform_key]
    raise RuntimeError(_CHROME_NOT_INSTALLED_MESSAGE.format(instructions=instructions))


# ---------------------------------------------------------------------------
# Profile directories
# ---------------------------------------------------------------------------


def profile_dir(name: str) -> Path:
    """Per-integration persistent browser profile directory.

    Returns ``data/playwright-profiles/{name}/``, creating it if needed.
    """
    d = project_root() / "data" / "playwright-profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Display detection
# ---------------------------------------------------------------------------


def resolve_executables(*names: str) -> dict[str, str]:
    """Return absolute executable paths, collecting all missing tools."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        path = shutil.which(name)
        if path is None:
            missing.append(name)
        else:
            resolved[name] = path
    if missing:
        raise RuntimeError(", ".join(missing))
    return resolved


def has_display() -> bool:
    """Return True if a working X display is available."""
    if not os.environ.get("DISPLAY"):
        return False
    xdpyinfo = shutil.which("xdpyinfo")
    if xdpyinfo is None:
        return False
    try:
        r = subprocess.run(  # noqa: S603 - fixed argv to resolved xdpyinfo path.
            [xdpyinfo],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    else:
        return r.returncode == 0


def display_is_live(display: str) -> bool:
    """Check if a specific X display is already responding."""
    xdpyinfo = shutil.which("xdpyinfo")
    if xdpyinfo is None:
        return False
    try:
        r = subprocess.run(  # noqa: S603 - fixed argv to resolved xdpyinfo path.
            [xdpyinfo],
            capture_output=True,
            timeout=3,
            env={**os.environ, "DISPLAY": display},
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    else:
        return r.returncode == 0


def _is_process_running(name: str) -> bool:
    """Check if a process with the given name is running (via pgrep)."""
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return False
    return (
        subprocess.run(  # noqa: S603 - fixed argv to resolved pgrep path.
            [pgrep, "-x", name],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


# ---------------------------------------------------------------------------
# Virtual display (Xvfb + x11vnc + noVNC)
# ---------------------------------------------------------------------------

_XVFB_DISPLAY = ":99"
_VNC_PORT = 5999
_NOVNC_PORT = 6080
_NOVNC_WEB_DIR = "/usr/share/novnc"


def _resolve_novnc_url() -> str:
    """Build the noVNC URL using the real hostname."""
    host = socket.gethostname()
    return f"http://{host}:{_NOVNC_PORT}/vnc.html?autoconnect=true"


def launch_display_processes(
    tool_paths: dict[str, str],
) -> list[subprocess.Popen[bytes]]:
    """Launch display tools in order, rolling back every partial startup."""
    commands = {
        "Xvfb": [_XVFB_DISPLAY, "-screen", "0", "1280x720x24"],
        "x11vnc": [
            "-display",
            _XVFB_DISPLAY,
            "-forever",
            "-nopw",
            "-rfbport",
            str(_VNC_PORT),
            "-quiet",
        ],
        "websockify": [str(_NOVNC_PORT), f"localhost:{_VNC_PORT}"],
    }
    if Path(_NOVNC_WEB_DIR).is_dir():
        commands["websockify"][0:0] = ["--web", _NOVNC_WEB_DIR]

    procs: list[subprocess.Popen[bytes]] = []
    with contextlib.ExitStack() as stack:
        stack.callback(stop_procs, procs)
        for name, executable in tool_paths.items():
            process = subprocess.Popen(  # noqa: S603 - fixed argv to resolved display executable.
                [executable, *commands[name]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            procs.append(process)
            time.sleep(0.5)
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited immediately (code {process.returncode})")
        stack.pop_all()
    return procs


def ensure_vnc_stack_alive() -> list[subprocess.Popen[bytes]]:
    """Repair missing VNC processes; the caller owns only the returned processes."""
    missing = [name for name in ("x11vnc", "websockify") if not _is_process_running(name)]
    return launch_display_processes(resolve_executables(*missing))


def start_virtual_display() -> tuple[list[subprocess.Popen[bytes]], str]:
    """Start Xvfb + x11vnc + noVNC.  Returns (processes, novnc_url).

    If display :99 is already running, reuses it and repairs the VNC stack
    if x11vnc or websockify died independently.

    Requires system packages: ``apt install xvfb x11vnc novnc``
    """
    try:
        tool_paths = resolve_executables("Xvfb", "x11vnc", "websockify")
    except RuntimeError as exc:
        raise RuntimeError(_HEADLESS_DISPLAY_REQUIRED_MESSAGE.format(error=exc)) from exc

    novnc_url = _resolve_novnc_url()

    # Reuse existing display if it's already running
    if display_is_live(_XVFB_DISPLAY):
        os.environ["DISPLAY"] = _XVFB_DISPLAY
        repair_procs = ensure_vnc_stack_alive()
        return repair_procs, novnc_url

    procs = launch_display_processes(tool_paths)
    os.environ["DISPLAY"] = _XVFB_DISPLAY
    return procs, novnc_url


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


def stop_procs(procs: list[subprocess.Popen[bytes]]) -> None:
    """Terminate processes gracefully, then force-kill stragglers."""
    for proc in reversed(procs):
        if proc.poll() is None:
            proc.terminate()
    for proc in reversed(procs):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


# ---------------------------------------------------------------------------
# Lock-file cleanup
# ---------------------------------------------------------------------------


def cleanup_lock_files(profile: Path) -> None:
    """Remove stale Chromium lock files from a profile directory.

    A browser crash or unclean shutdown can leave these behind,
    preventing the next persistent context from launching.
    """
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = profile / name
        if lock.exists() or lock.is_symlink():
            with contextlib.suppress(OSError):
                lock.unlink()


# ---------------------------------------------------------------------------
# System dependency checks
# ---------------------------------------------------------------------------


def check_browser_plugin_deps(service_name: str) -> None:
    """Check Chrome + VNC deps for a browser plugin. Logs warnings only."""
    try:
        chrome_path()
    except RuntimeError as e:
        logger.warning("system dep check failed", service_name=service_name, error=str(e))
        return
    if not os.environ.get("DISPLAY"):
        missing = [t for t in ("Xvfb", "x11vnc", "websockify") if not shutil.which(t)]
        if missing:
            logger.warning(
                "headless server needs VNC deps", service_name=service_name, missing=missing
            )
