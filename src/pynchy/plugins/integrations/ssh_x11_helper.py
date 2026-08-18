#!/usr/bin/env python3
"""Allowlisted JSON bridge for controlling the active local X11 desktop."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess  # noqa: S404 - allowlisted argv only; no shell execution.
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROTOCOL_VERSION = 1
SUPPORTED_ACTIONS = frozenset(
    {
        "capture",
        "list_apps",
        "list_windows",
        "click",
        "double_click",
        "right_click",
        "type",
        "key",
        "launch_app",
        "scroll",
        "check_permissions",
    }
)
_BUTTONS = {"click": "1", "double_click": "1", "right_click": "3"}
_KEY_NAMES = {"cmd": "super", "command": "super", "option": "alt", "control": "ctrl"}


def command(request: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0911,PLR0912 - closed dispatch.
    """Execute one validated, allowlisted desktop action."""
    action = request.get("action")
    if action not in SUPPORTED_ACTIONS:
        raise ValueError(f"SSH X11 helper does not implement {action}")
    env = _x11_environment()
    if action == "check_permissions":
        _require_binaries("xdotool", "wmctrl", "import", path=env["PATH"])
        active = _run(["xdotool", "getactivewindow"], env=env).stdout.decode().strip()
        return {
            "protocol_version": PROTOCOL_VERSION,
            "supported_actions": sorted(SUPPORTED_ACTIONS),
            "display": env["DISPLAY"],
            "active_window": active,
            "ready": True,
        }
    if action == "launch_app":
        return _launch_urls(request, env)

    windows = _windows(env)
    if action == "list_apps":
        apps = sorted({window["class"] for window in windows if window["class"]})
        return {"apps": apps}
    if action == "list_windows":
        return {"windows": _matching_windows(windows, request)}

    target = _target_window(windows, request)
    if target is not None:
        _run(["wmctrl", "-ia", target["id"]], env=env)
        time.sleep(float(env.get("PYNCHY_X11_FOCUS_DELAY_SECONDS", "0.25")))

    if action == "capture":
        image = _run(["import", "-silent", "-window", "root", "png:-"], env=env).stdout
        return {
            "window": target,
            "screenshot_png_base64": base64.b64encode(image).decode(),
        }
    if action in _BUTTONS:
        coordinate = request.get("coordinate")
        if not isinstance(coordinate, list) or len(coordinate) != 2:
            raise ValueError("SSH X11 clicks require coordinate=[x,y]")
        clicks = "2" if action == "double_click" else "1"
        _run(
            [
                "xdotool",
                "mousemove",
                "--sync",
                str(coordinate[0]),
                str(coordinate[1]),
                "click",
                "--repeat",
                clicks,
                _BUTTONS[action],
            ],
            env=env,
        )
        return {"coordinate": coordinate}
    if action == "type":
        if request.get("clear"):
            _run(["xdotool", "key", "--clearmodifiers", "ctrl+a"], env=env)
        _run(
            ["xdotool", "type", "--clearmodifiers", "--delay", "1", "--", request["text"]],
            env=env,
        )
        return {"typed": True}
    if action == "key":
        raw_keys = request["keys"]
        keys = raw_keys.split("+") if isinstance(raw_keys, str) else raw_keys
        chord = "+".join(_KEY_NAMES.get(key.lower(), key.lower()) for key in keys)
        _run(["xdotool", "key", "--clearmodifiers", chord], env=env)
        return {"keys": chord}

    direction = request.get("direction")
    if direction is None:
        direction = "down" if request.get("delta_y", 0) < 0 else "up"
    button = {"up": "4", "down": "5", "left": "6", "right": "7"}[direction]
    amount = request.get("amount") or max(1, abs(request.get("delta_y", 0)) // 120)
    _run(["xdotool", "click", "--repeat", str(amount), button], env=env)
    return {"direction": direction, "amount": amount}


def _launch_urls(request: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    urls = request.get("urls")
    if not isinstance(urls, list) or not urls:
        raise ValueError("SSH X11 launch_app requires at least one URL")
    for url in urls:
        parsed = urlsplit(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("SSH X11 launch_app requires HTTP(S) URLs")
        _run(["xdg-open", url], env=env)
    return {"launched": True, "urls": urls}


def _x11_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XAUTHORITY", str(Path("~/.Xauthority").expanduser()))
    local_root = Path("~/.local/opt/xdotool/usr").expanduser()
    if local_root.is_dir():
        env["PATH"] = f"{local_root}/bin:{env.get('PATH', '')}"
        env["LD_LIBRARY_PATH"] = (
            f"{local_root}/lib/x86_64-linux-gnu:{env.get('LD_LIBRARY_PATH', '')}"
        )
    return env


def _require_binaries(*names: str, path: str | None = None) -> None:
    missing = [name for name in names if shutil.which(name, path=path) is None]
    if missing:
        raise RuntimeError(f"missing desktop binaries: {', '.join(missing)}")


def _run(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    _require_binaries(argv[0], path=env["PATH"])
    return subprocess.run(  # noqa: S603 - every executable and subcommand is allowlisted above.
        argv,
        check=True,
        capture_output=True,
        env=env,
        timeout=20,
    )


def _windows(env: dict[str, str]) -> list[dict[str, Any]]:
    lines = _run(["wmctrl", "-lxp"], env=env).stdout.decode(errors="replace").splitlines()
    windows: list[dict[str, Any]] = []
    for line in lines:
        parts = line.split(maxsplit=5)
        if len(parts) < 6:
            continue
        window_id, desktop, pid, window_class, host, title = parts
        windows.append(
            {
                "id": window_id,
                "window_id": int(window_id, 16),
                "desktop": int(desktop),
                "pid": int(pid),
                "class": window_class,
                "host": host,
                "title": title,
            }
        )
    return windows


def _matching_windows(
    windows: list[dict[str, Any]], request: dict[str, Any]
) -> list[dict[str, Any]]:
    app = str(request.get("app", "")).casefold()
    title = str(request.get("window_title", "")).casefold()
    pid = request.get("pid")
    window_id = request.get("window_id")
    return [
        window
        for window in windows
        if (not app or app in f"{window['class']} {window['title']}".casefold())
        and (not title or title in window["title"].casefold())
        and (pid is None or pid == window["pid"])
        and (window_id is None or window_id == window["window_id"])
    ]


def _target_window(windows: list[dict[str, Any]], request: dict[str, Any]) -> dict[str, Any] | None:
    if not any(
        request.get(field) is not None for field in ("app", "window_title", "pid", "window_id")
    ):
        return None
    matches = _matching_windows(windows, request)
    if not matches:
        raise ValueError("no matching X11 window")
    index = request.get("window_index", 0)
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("window_index must be a non-negative integer")
    if index >= len(matches):
        raise ValueError(f"window_index {index} exceeds {len(matches)} matching windows")
    return matches[index]


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise TypeError("request must be a JSON object")
        response = command(request)
    except (
        KeyError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as exc:
        response = {"error": str(exc)}
    json.dump(response, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 1 if "error" in response else 0


if __name__ == "__main__":
    raise SystemExit(main())
