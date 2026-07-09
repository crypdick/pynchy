"""Persistent Xvfb + noVNC display lifecycle for the X integration.

X tools always use headed mode to avoid bot detection, so Xvfb persists for
the lifetime of this plugin on headless hosts.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import time
from pathlib import Path

from pynchy.plugins.integrations.browser import has_display, stop_procs

XVFB_DISPLAY = ":99"
_VNC_PORT = 5999
_NOVNC_PORT = 6080
_NOVNC_WEB_DIR = "/usr/share/novnc"

# Module-level Xvfb process.  X tools use headed mode to avoid bot detection,
# so Xvfb persists for the lifetime of this plugin on headless hosts.
_xvfb_proc: subprocess.Popen[bytes] | None = None


def ensure_xvfb() -> None:
    """Ensure Xvfb is running. X needs headed mode to avoid bot detection.

    Starts Xvfb once and keeps it running for the lifetime of the server.
    Safe to call multiple times — subsequent calls are no-ops if Xvfb is
    already running or a native display is available.
    """
    global _xvfb_proc
    if has_display():
        return
    if _xvfb_proc is not None and _xvfb_proc.poll() is None:
        os.environ["DISPLAY"] = XVFB_DISPLAY
        return
    if not shutil.which("Xvfb"):
        raise RuntimeError(
            "No display available and Xvfb not installed. X automation requires "
            "headed mode to avoid bot detection. Install with: apt install xvfb"
        )
    _xvfb_proc = subprocess.Popen(
        ["Xvfb", XVFB_DISPLAY, "-screen", "0", "1280x720x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    if _xvfb_proc.poll() is not None:
        code = _xvfb_proc.returncode
        _xvfb_proc = None
        raise RuntimeError(f"Xvfb exited immediately (code {code})")
    os.environ["DISPLAY"] = XVFB_DISPLAY


def start_vnc_layer() -> tuple[list[subprocess.Popen[bytes]], str]:
    """Start x11vnc + noVNC on the existing Xvfb display.

    Returns (processes, novnc_url).  Call ``ensure_xvfb()`` first.
    """
    missing = [t for t in ("x11vnc", "websockify") if not shutil.which(t)]
    if missing:
        raise RuntimeError(
            f"VNC layer requires: {', '.join(missing)}. Install with: apt install x11vnc novnc"
        )
    procs: list[subprocess.Popen[bytes]] = []
    try:
        x11vnc = subprocess.Popen(
            [
                "x11vnc",
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

        ws_cmd = ["websockify", str(_NOVNC_PORT), f"localhost:{_VNC_PORT}"]
        if Path(_NOVNC_WEB_DIR).is_dir():
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

    except Exception:
        stop_procs(procs)
        raise
    else:
        return procs, f"http://HOST:{_NOVNC_PORT}/vnc.html?autoconnect=true"


def cleanup_xvfb() -> None:
    global _xvfb_proc
    if _xvfb_proc and _xvfb_proc.poll() is None:
        _xvfb_proc.terminate()
        try:
            _xvfb_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _xvfb_proc.kill()
    _xvfb_proc = None


atexit.register(cleanup_xvfb)
