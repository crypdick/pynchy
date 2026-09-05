"""Persistent Xvfb + noVNC display lifecycle for the X integration.

X tools always use headed mode to avoid bot detection, so Xvfb persists for
the lifetime of this plugin on headless hosts.
"""

from __future__ import annotations

import atexit
import os
import subprocess  # noqa: S404 - fixed argv process helpers; never uses shell=True.
from dataclasses import dataclass

from pynchy.plugins.integrations.browser import (
    has_display,
    launch_display_processes,
    resolve_executables,
)

XVFB_DISPLAY = ":99"
_NOVNC_PORT = 6080
_XVFB_NOT_INSTALLED = (
    "No display available and Xvfb not installed. X automation requires "
    "headed mode to avoid bot detection. Install with: apt install xvfb"
)
_VNC_REQUIREMENTS_MISSING = "VNC layer requires: {missing}. Install with: apt install x11vnc novnc"


@dataclass(slots=True)
class _DisplayState:
    xvfb_proc: subprocess.Popen[bytes] | None = None


_state = _DisplayState()


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
        tool_paths = resolve_executables("Xvfb")
    except RuntimeError as exc:
        raise RuntimeError(_XVFB_NOT_INSTALLED) from exc
    _state.xvfb_proc = launch_display_processes(tool_paths)[0]
    os.environ["DISPLAY"] = XVFB_DISPLAY


def start_vnc_layer() -> tuple[list[subprocess.Popen[bytes]], str]:
    """Start x11vnc + noVNC on the existing Xvfb display.

    Returns (processes, novnc_url).  Call ``ensure_xvfb()`` first.
    """
    try:
        tool_paths = resolve_executables("x11vnc", "websockify")
    except RuntimeError as exc:
        raise RuntimeError(_VNC_REQUIREMENTS_MISSING.format(missing=exc)) from exc
    procs = launch_display_processes(tool_paths)
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
