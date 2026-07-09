"""Host-side computer_use tool backed by Cua Driver."""

from __future__ import annotations

import asyncio
import json
import platform
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pluggy

from pynchy.config import get_settings

hookimpl = pluggy.HookimplMarker("pynchy")

_CONTAINER_ARTIFACT_DIR = "/workspace/ipc/computer-use"
_PASSTHROUGH_CONTROL_KEYS = {
    "type",
    "request_id",
    "source_group",
    "action",
    "capture_after",
    "label",
    "element",
    "coordinate",
    "keys",
}
_VALID_ACTIONS = {
    "capture",
    "list_apps",
    "list_windows",
    "launch_app",
    "click",
    "double_click",
    "right_click",
    "type",
    "key",
    "scroll",
    "wait",
    "check_permissions",
}


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _slug(value: object) -> str:
    if not isinstance(value, str):
        return "computer-use"
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "computer-use"


def _source_group(data: dict[str, Any]) -> str | None:
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        return None
    parts = Path(source_group).parts
    if len(parts) != 1 or parts[0] in {".", ".."}:
        return None
    return source_group


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _action(data: dict[str, Any]) -> str:
    action = data.get("action")
    if not isinstance(action, str) or action not in _VALID_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(sorted(_VALID_ACTIONS))}")
    return action


def _artifact_path(*, source_group: str, label: object) -> Path:
    filename = f"{_timestamp()}-{_slug(label)}.png"
    return get_settings().data_dir / "ipc" / source_group / "computer-use" / filename


def _window_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"pid": _positive_int(data.get("pid"), "pid")}
    window_id = _optional_positive_int(data.get("window_id"), "window_id")
    if window_id is not None:
        payload["window_id"] = window_id
    query = data.get("query")
    if query is not None:
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")
        payload["query"] = query
    return payload


def _base_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if key not in _PASSTHROUGH_CONTROL_KEYS and value is not None
    }


def _coordinate(data: dict[str, Any]) -> tuple[int, int] | None:
    raw = data.get("coordinate")
    if raw is None:
        return None
    if (
        not isinstance(raw, list)
        or len(raw) != 2
        or any(isinstance(part, bool) or not isinstance(part, int) or part < 0 for part in raw)
    ):
        raise ValueError("coordinate must be [x, y] with non-negative integer values")
    return raw[0], raw[1]


def _keys(data: dict[str, Any]) -> list[str]:
    raw = data.get("keys")
    if isinstance(raw, str):
        keys = [part.strip().lower() for part in raw.split("+") if part.strip()]
    elif isinstance(raw, list) and all(isinstance(part, str) and part for part in raw):
        keys = [part.lower() for part in raw]
    else:
        raise ValueError("keys must be a shortcut string or a list of key names")
    if not keys:
        raise ValueError("keys must include at least one key")
    return keys


def _cua_action_and_payload(action: str, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if action == "capture":
        return "get_window_state", _window_payload(data)
    if action == "type":
        text = data.get("text")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        payload = _base_payload(data)
        payload["text"] = text
        return "type_text", payload
    if action == "key":
        payload = _base_payload(data)
        payload["keys"] = _keys(data)
        return "hotkey", payload
    if action in {"click", "double_click", "right_click"}:
        payload = _base_payload(data)
        element = data.get("element")
        if element is not None:
            payload["element_index"] = _positive_int(element, "element")
        if coord := _coordinate(data):
            payload["x"], payload["y"] = coord
        if "element_index" not in payload and ("x" not in payload or "y" not in payload):
            raise ValueError("click actions require element or coordinate")
        return action, payload
    if action == "wait":
        seconds = data.get("seconds", 1.0)
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or seconds < 0:
            raise ValueError("seconds must be a non-negative number")
        return action, {"seconds": seconds}
    return action, _base_payload(data)


async def _run_cua(
    *,
    binary: str,
    action: str,
    payload: dict[str, Any],
    screenshot_path: Path | None = None,
) -> dict[str, Any]:
    command = [binary, "call", action]
    if payload:
        command.append(json.dumps(payload, separators=(",", ":")))
    if screenshot_path is not None:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--screenshot-out-file", str(screenshot_path)])

    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        error = stderr.decode(errors="replace").strip() or output or f"exit code {proc.returncode}"
        raise RuntimeError(f"cua-driver {action} failed: {error}")

    result: dict[str, Any] = {
        "cua_action": action,
        "output": output,
    }
    if screenshot_path is not None:
        if not screenshot_path.exists():
            raise RuntimeError(f"cua-driver {action} did not create the screenshot file")
        result["screenshot"] = {
            "host_path": str(screenshot_path),
            "container_path": f"{_CONTAINER_ARTIFACT_DIR}/{screenshot_path.name}",
            "format": "png",
            "bytes": screenshot_path.stat().st_size,
        }
    return result


async def _handle_computer_use(data: dict[str, Any]) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"error": "computer_use is only supported on macOS hosts."}

    source_group = _source_group(data)
    if source_group is None:
        return {"error": "Missing or invalid source group for computer_use request."}

    binary = shutil.which("cua-driver")
    if binary is None:
        return {
            "error": (
                "cua-driver is not installed on the host; install Cua Driver before using "
                "computer_use."
            )
        }

    try:
        action = _action(data)
        if action == "wait":
            seconds = _cua_action_and_payload(action, data)[1]["seconds"]
            await asyncio.sleep(seconds)
            return {"result": {"action": action, "output": f"waited {seconds:g}s"}}

        cua_action, payload = _cua_action_and_payload(action, data)
        screenshot_path = None
        if action == "capture":
            screenshot_path = _artifact_path(source_group=source_group, label=data.get("label"))

        result = await _run_cua(
            binary=binary,
            action=cua_action,
            payload=payload,
            screenshot_path=screenshot_path,
        )
        result["action"] = action

        if data.get("capture_after") is True and {"pid", "window_id"} <= payload.keys():
            after_path = _artifact_path(source_group=source_group, label=f"after-{action}")
            after = await _run_cua(
                binary=binary,
                action="get_window_state",
                payload={"pid": payload["pid"], "window_id": payload["window_id"]},
                screenshot_path=after_path,
            )
            result["after"] = after
        return {"result": result}
    except (RuntimeError, ValueError) as exc:
        return {"error": str(exc)}


class ComputerUsePlugin:
    """Expose host-mediated computer-use actions through Cua Driver."""

    @hookimpl
    def pynchy_service_handler(self) -> dict[str, Any]:
        return {"tools": {"computer_use": _handle_computer_use}}

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        skill_dir = (
            Path(__file__).resolve().parent.parent.parent / "agent" / "skills" / "computer-use"
        )
        if skill_dir.is_dir():
            return [str(skill_dir)]
        return []
