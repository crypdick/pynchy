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
import subprocess
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
            raise RuntimeError(
                f"CHROME_PATH={path!r} does not exist. Install Chrome/Chromium "
                "and update CHROME_PATH in .env."
            )
        return path

    # 2. Auto-detect
    detected = _detect_chrome()
    if detected:
        return detected

    # 3. Not found — give platform-specific install instructions
    platform_key = "darwin" if sys.platform == "darwin" else "linux"
    instructions = _INSTALL_INSTRUCTIONS[platform_key]
    raise RuntimeError(
        "Chrome/Chromium is not installed (or not in a standard location).\n"
        "\n"
        f"{instructions}\n"
        "\n"
        "After installing, either ensure the binary is in a standard path or "
        "set CHROME_PATH in .env to point to it."
    )


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


def has_display() -> bool:
    """Return True if a working X display is available."""
    if not os.environ.get("DISPLAY"):
        return False
    try:
        r = subprocess.run(["xdpyinfo"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def display_is_live(display: str) -> bool:
    """Check if a specific X display is already responding."""
    try:
        r = subprocess.run(
            ["xdpyinfo"],
            capture_output=True,
            timeout=3,
            env={**os.environ, "DISPLAY": display},
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_process_running(name: str) -> bool:
    """Check if a process with the given name is running (via pgrep)."""
    return subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# Virtual display (Xvfb + x11vnc + noVNC)
# ---------------------------------------------------------------------------

_XVFB_DISPLAY = ":99"
_VNC_PORT = 5999
_NOVNC_PORT = 6080
_NOVNC_WEB_DIR = "/usr/share/novnc"


def _resolve_novnc_url() -> str:
    """Build the noVNC URL using the real hostname."""
    import socket

    host = socket.gethostname()
    return f"http://{host}:{_NOVNC_PORT}/vnc.html?autoconnect=true"


def ensure_vnc_stack_alive() -> list[subprocess.Popen]:
    """Restart x11vnc and/or websockify if they died while Xvfb is still up.

    Returns list of newly started processes (caller should track for cleanup).
    """
    procs: list[subprocess.Popen] = []

    if not _is_process_running("x11vnc"):
        p = subprocess.Popen(
            [
                "x11vnc",
                "-display",
                _XVFB_DISPLAY,
                "-forever",
                "-nopw",
                "-rfbport",
                str(_VNC_PORT),
                "-quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p)
        time.sleep(0.5)

    if not _is_process_running("websockify"):
        ws_cmd = ["websockify", str(_NOVNC_PORT), f"localhost:{_VNC_PORT}"]
        if os.path.isdir(_NOVNC_WEB_DIR):
            ws_cmd[1:1] = ["--web", _NOVNC_WEB_DIR]
        p = subprocess.Popen(ws_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        time.sleep(0.5)

    return procs


def start_virtual_display() -> tuple[list[subprocess.Popen], str]:
    """Start Xvfb + x11vnc + noVNC.  Returns (processes, novnc_url).

    If display :99 is already running, reuses it and repairs the VNC stack
    if x11vnc or websockify died independently.

    Requires system packages: ``apt install xvfb x11vnc novnc``
    """
    missing = [t for t in ("Xvfb", "x11vnc", "websockify") if not shutil.which(t)]
    if missing:
        raise RuntimeError(
            f"Headless display requires: {', '.join(missing)}. "
            "Install with: apt install xvfb x11vnc novnc"
        )

    novnc_url = _resolve_novnc_url()

    # Reuse existing display if it's already running
    if display_is_live(_XVFB_DISPLAY):
        os.environ["DISPLAY"] = _XVFB_DISPLAY
        repair_procs = ensure_vnc_stack_alive()
        return repair_procs, novnc_url

    procs: list[subprocess.Popen] = []
    try:
        xvfb = subprocess.Popen(
            ["Xvfb", _XVFB_DISPLAY, "-screen", "0", "1280x720x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(xvfb)
        time.sleep(0.5)
        if xvfb.poll() is not None:
            raise RuntimeError(f"Xvfb exited immediately (code {xvfb.returncode})")

        x11vnc = subprocess.Popen(
            [
                "x11vnc",
                "-display",
                _XVFB_DISPLAY,
                "-forever",
                "-nopw",
                "-rfbport",
                str(_VNC_PORT),
                "-quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(x11vnc)
        time.sleep(0.5)
        if x11vnc.poll() is not None:
            raise RuntimeError(f"x11vnc exited immediately (code {x11vnc.returncode})")

        ws_cmd = ["websockify", str(_NOVNC_PORT), f"localhost:{_VNC_PORT}"]
        if os.path.isdir(_NOVNC_WEB_DIR):
            ws_cmd[1:1] = ["--web", _NOVNC_WEB_DIR]
        websockify_proc = subprocess.Popen(
            ws_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(websockify_proc)
        time.sleep(0.5)
        if websockify_proc.poll() is not None:
            raise RuntimeError(f"websockify exited immediately (code {websockify_proc.returncode})")

        os.environ["DISPLAY"] = _XVFB_DISPLAY
        return procs, novnc_url

    except Exception:
        stop_procs(procs)
        raise


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


def stop_procs(procs: list[subprocess.Popen]) -> None:
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

    A previous browser crash or unclean shutdown can leave these behind,
    preventing the next persistent context from launching.
    """
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = profile / name
        if lock.exists():
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
        logger.warning(f"{service_name} system dep check failed", error=str(e))
        return
    if not os.environ.get("DISPLAY"):
        missing = [t for t in ("Xvfb", "x11vnc", "websockify") if not shutil.which(t)]
        if missing:
            logger.warning(f"Headless server — {service_name} needs VNC deps", missing=missing)
