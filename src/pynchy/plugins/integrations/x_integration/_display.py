"""Persistent Xvfb + noVNC display lifecycle for the X integration.

X tools always use headed mode to avoid bot detection, so Xvfb persists for
the lifetime of this plugin on headless hosts.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess  # noqa: S404, RUF100 - fixed argv process helpers; never uses shell=True.
import time
from dataclasses import dataclass
from pathlib import Path

from pynchy.plugins.integrations.browser import has_display, stop_procs

XVFB_DISPLAY = ":99"
_VNC_PORT = 5999
_NOVNC_PORT = 6080
_NOVNC_WEB_DIR = "/usr/share/novnc"


@dataclass(slots=True)
class _DisplayState:
    xvfb_proc: subprocess.Popen[bytes] | None = None


_state = _DisplayState()


def _resolve_executable(name: str) -> str:
    """Return an absolute executable path from PATH or raise a clear error."""
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(name)
    return path


def _resolve_executables(*names: str) -> dict[str, str]:
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


def ensure_xvfb() -> None:
    """Ensure Xvfb is running. X needs headed mode to avoid bot detection.

    Starts Xvfb once and keeps it running for the lifetime of the server.
    Safe to call multiple times — subsequent calls are no-ops if Xvfb is
    already running or a native display is available.
    """
    if has_display():
        return
    if _state.xvfb_proc is not None and _state.xvfb_proc.poll() is None:
        os.environ["DISPLAY"] = XVFB_DISPLAY
        return
    try:
        xvfb_path = _resolve_executable("Xvfb")
    except RuntimeError as exc:
        raise RuntimeError(
            "No display available and Xvfb not installed. X automation requires "
            "headed mode to avoid bot detection. Install with: apt install xvfb"
        ) from exc
    _state.xvfb_proc = subprocess.Popen(  # noqa: S603, RUF100 - fixed argv to resolved Xvfb path.
        [xvfb_path, XVFB_DISPLAY, "-screen", "0", "1280x720x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    if _state.xvfb_proc.poll() is not None:
        code = _state.xvfb_proc.returncode
        _state.xvfb_proc = None
        raise RuntimeError(f"Xvfb exited immediately (code {code})")
    os.environ["DISPLAY"] = XVFB_DISPLAY


def start_vnc_layer() -> tuple[list[subprocess.Popen[bytes]], str]:
    """Start x11vnc + noVNC on the existing Xvfb display.

    Returns (processes, novnc_url).  Call ``ensure_xvfb()`` first.
    """
    try:
        tool_paths = _resolve_executables("x11vnc", "websockify")
    except RuntimeError as exc:
        raise RuntimeError(
            f"VNC layer requires: {exc}. Install with: apt install x11vnc novnc"
        ) from exc
    procs: list[subprocess.Popen[bytes]] = []
    try:
        x11vnc = subprocess.Popen(  # noqa: S603, RUF100 - fixed argv to resolved x11vnc path.
            [
                tool_paths["x11vnc"],
                "-display",
                XVFB_DISPLAY,
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

        ws_cmd = [tool_paths["websockify"], str(_NOVNC_PORT), f"localhost:{_VNC_PORT}"]
        if Path(_NOVNC_WEB_DIR).is_dir():
            ws_cmd[1:1] = ["--web", _NOVNC_WEB_DIR]
        websockify_proc = subprocess.Popen(  # noqa: S603, RUF100 - fixed argv to resolved path.
            ws_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(websockify_proc)
        time.sleep(0.5)
        if websockify_proc.poll() is not None:
            raise RuntimeError(f"websockify exited immediately (code {websockify_proc.returncode})")

    except Exception:
        stop_procs(procs)
        raise
    else:
        return procs, f"http://HOST:{_NOVNC_PORT}/vnc.html?autoconnect=true"


def cleanup_xvfb() -> None:
    if _state.xvfb_proc and _state.xvfb_proc.poll() is None:
        _state.xvfb_proc.terminate()
        try:
            _state.xvfb_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _state.xvfb_proc.kill()
    _state.xvfb_proc = None


atexit.register(cleanup_xvfb)
