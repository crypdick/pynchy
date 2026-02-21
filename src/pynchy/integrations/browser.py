"""Shared Playwright browser utilities for integration plugins.

Extracted from the Slack token extractor and X integration scripts to
eliminate duplication.  These are plain functions — plugin-specific logic
(anti-detection args, persistent Xvfb) stays in the respective plugin modules.

Design decision: CHROME_PATH required
--------------------------------------
All browser plugins use the system Chrome/Chromium binary (``CHROME_PATH``)
rather than Playwright's vendored Chromium.  Playwright is used only for its
automation protocol (CDP), never for its bundled browser.

Rationale: multiple services (X/Twitter in particular) actively fingerprint
Playwright's Chromium build and block it as bot traffic.  Using the host's
Chrome installation produces a genuine browser fingerprint.  We enforce this
policy uniformly across all plugins rather than per-service to avoid
accidentally shipping a detectable fingerprint if a new integration is added.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    root = os.environ.get("PYNCHY_PROJECT_ROOT", "")
    return Path(root) if root else Path.cwd()


# ---------------------------------------------------------------------------
# System Chrome (never use Playwright's vendored browser)
# ---------------------------------------------------------------------------


def chrome_path() -> str:
    """Return the system Chrome/Chromium path from the ``CHROME_PATH`` env var.

    All browser plugins must use the system browser.  Playwright's vendored
    Chromium has a distinct fingerprint that services (notably X) detect and
    block as bot traffic.  Playwright is used only for its CDP automation
    protocol, never for its bundled browser binary.

    Raises ``RuntimeError`` if ``CHROME_PATH`` is unset or the path doesn't exist.
    """
    path = os.environ.get("CHROME_PATH", "")
    if not path:
        raise RuntimeError(
            "CHROME_PATH is required. Set it to the system Chrome/Chromium binary "
            "path in .env (e.g. CHROME_PATH=/usr/bin/google-chrome-stable). "
            "Playwright's bundled Chromium is never used — services fingerprint "
            "it as bot traffic."
        )
    if not Path(path).is_file():
        raise RuntimeError(
            f"CHROME_PATH={path!r} does not exist. Install Chrome/Chromium and "
            "update CHROME_PATH in .env."
        )
    return path


# ---------------------------------------------------------------------------
# Profile directories
# ---------------------------------------------------------------------------


def profile_dir(name: str) -> Path:
    """Per-integration persistent browser profile directory.

    Returns ``data/playwright-profiles/{name}/``, creating it if needed.
    """
    d = _project_root() / "data" / "playwright-profiles" / name
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


# ---------------------------------------------------------------------------
# Virtual display (Xvfb + x11vnc + noVNC)
# ---------------------------------------------------------------------------

_XVFB_DISPLAY = ":99"
_VNC_PORT = 5999
_NOVNC_PORT = 6080
_NOVNC_WEB_DIR = "/usr/share/novnc"


def start_virtual_display() -> tuple[list[subprocess.Popen], str]:
    """Start Xvfb + x11vnc + noVNC.  Returns (processes, novnc_url).

    Requires system packages: ``apt install xvfb x11vnc novnc``
    """
    missing = [t for t in ("Xvfb", "x11vnc", "websockify") if not shutil.which(t)]
    if missing:
        raise RuntimeError(
            f"Headless display requires: {', '.join(missing)}. "
            "Install with: apt install xvfb x11vnc novnc"
        )

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
        return procs, f"http://HOST:{_NOVNC_PORT}/vnc.html?autoconnect=true"

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


def check_system_deps(needed: list[str]) -> list[str]:
    """Return the subset of *needed* binaries that are missing from PATH."""
    return [name for name in needed if not shutil.which(name)]
