"""Host-side desktop screenshot tool for macOS."""

from __future__ import annotations

import asyncio
import platform
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pluggy

from pynchy.config import get_settings

hookimpl = pluggy.HookimplMarker("pynchy")

_SCREENSHOT_BIN = "/usr/sbin/screencapture"
_CONTAINER_SCREENSHOT_DIR = "/workspace/ipc/screenshots"
_VALID_MODES = {"full", "selection", "window"}


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _slug(value: object) -> str:
    if not isinstance(value, str):
        return "screenshot"
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "screenshot"


def _source_group(data: dict[str, Any]) -> str | None:
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        return None
    parts = Path(source_group).parts
    if len(parts) != 1 or parts[0] in {".", ".."}:
        return None
    return source_group


def _mode(data: dict[str, Any]) -> str | None:
    mode = data.get("mode", "full")
    if not isinstance(mode, str) or mode not in _VALID_MODES:
        return None
    return mode


def _display_id(data: dict[str, Any]) -> int | None:
    raw = data.get("display_id")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError("display_id must be a positive integer")
    return raw


def _screenshot_path(*, source_group: str, label: object) -> Path:
    filename = f"{_timestamp()}-{_slug(label)}.png"
    return get_settings().data_dir / "ipc" / source_group / "screenshots" / filename


def _command(data: dict[str, Any], output_path: Path, mode: str) -> list[str]:
    args = [_SCREENSHOT_BIN, "-x", "-t", "png"]
    if data.get("include_cursor") is True:
        args.append("-C")
    display_id = _display_id(data)
    if display_id is not None:
        args.extend(["-D", str(display_id)])
    if mode == "selection":
        args.extend(["-i", "-s"])
    elif mode == "window":
        args.extend(["-i", "-w"])
    args.append(str(output_path))
    return args


async def _handle_take_screenshot(data: dict[str, Any]) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"error": "Desktop screenshots are only supported on macOS hosts."}

    source_group = _source_group(data)
    if source_group is None:
        return {"error": "Missing or invalid source group for screenshot request."}

    mode = _mode(data)
    if mode is None:
        return {"error": 'mode must be one of "full", "selection", or "window".'}

    try:
        output_path = _screenshot_path(source_group=source_group, label=data.get("label"))
        command = _command(data, output_path, mode)
    except ValueError as exc:
        return {"error": str(exc)}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        error = stderr.decode(errors="replace").strip() or f"exit code {proc.returncode}"
        return {"error": f"screencapture failed: {error}"}
    if not output_path.exists():
        return {"error": "screencapture succeeded but did not create an output file."}

    return {
        "result": {
            "host_path": str(output_path),
            "container_path": f"{_CONTAINER_SCREENSHOT_DIR}/{output_path.name}",
            "format": "png",
            "mode": mode,
            "bytes": output_path.stat().st_size,
        }
    }


class DesktopScreenshotPlugin:
    """Expose a macOS desktop screenshot service tool."""

    @hookimpl
    def pynchy_service_handler(self) -> dict[str, Any]:
        return {"tools": {"take_screenshot": _handle_take_screenshot}}
