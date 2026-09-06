"""Peekaboo computer-use provider plugin for macOS."""

from __future__ import annotations

import asyncio
import json
import platform
import shutil
import subprocess  # noqa: S404 - resolved Peekaboo binary runs with closed argv.
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import pluggy
from pydantic import BaseModel, Field

from pynchy.plugins.api import (
    ComputerUseAction,
    ComputerUseBackend,
    ComputerUseBackendAvailability,
    ComputerUseRequest,
)
from pynchy.plugins.computer_use.artifacts import screenshot_artifact

if TYPE_CHECKING:
    from collections.abc import Callable

hookimpl = pluggy.HookimplMarker("pynchy")
PositiveTimeout = Annotated[float, Field(gt=0)]
_CLICK_ACTIONS = frozenset(
    {
        ComputerUseAction.CLICK,
        ComputerUseAction.DOUBLE_CLICK,
        ComputerUseAction.RIGHT_CLICK,
    }
)


class PeekabooConfig(BaseModel):
    """Plugin-owned Peekaboo executable and timeout configuration."""

    model_config = {"extra": "forbid"}

    # NOTE: Update docs/usage/host-capabilities/computer-use.md § Built-in: Peekaboo
    # if these defaults change.
    binary: Annotated[str, Field(min_length=1)] = "peekaboo"
    timeout_seconds: PositiveTimeout = 30.0


@dataclass(frozen=True)
class PeekabooBackend:
    """Translate the neutral request contract into allowlisted Peekaboo argv."""

    config: PeekabooConfig

    @property
    def name(self) -> str:
        return "peekaboo"

    def availability(self) -> ComputerUseBackendAvailability:
        if platform.system() != "Darwin":
            return ComputerUseBackendAvailability(
                available=False,
                reason="Peekaboo requires macOS",
            )
        if shutil.which(self.config.binary) is None:
            return ComputerUseBackendAvailability(
                available=False,
                reason=f"Peekaboo is not installed at {self.config.binary!r}",
            )
        return ComputerUseBackendAvailability(available=True)

    async def execute(
        self,
        request: ComputerUseRequest,
        *,
        screenshot_path: Path | None = None,
    ) -> dict[str, Any]:
        binary = shutil.which(self.config.binary)
        if binary is None:
            raise RuntimeError(f"Peekaboo is not installed at {self.config.binary!r}")
        command = [binary, *_peekaboo_command(request, screenshot_path=screenshot_path)]
        parsed = await _run_json_command(command, timeout_seconds=self.config.timeout_seconds)
        result: dict[str, Any] = {
            "backend": self.name,
            "peekaboo_action": request.action.value,
            "output": parsed.get("data", parsed),
        }
        if screenshot_path is not None:
            result["screenshot"] = await screenshot_artifact(screenshot_path)
        return result


class PeekabooComputerUsePlugin:
    """Contribute Peekaboo as an optional macOS computer-use provider."""

    def __init__(self, config: PeekabooConfig | None = None) -> None:
        self._config = config or PeekabooConfig()

    def configure(self, config: PeekabooConfig) -> None:
        """Apply the provider's resolved service configuration before registration."""
        self._config = config

    @hookimpl
    def pynchy_computer_use_backend(self) -> ComputerUseBackend:
        return PeekabooBackend(self._config)


def _peekaboo_command(request: ComputerUseRequest, *, screenshot_path: Path | None) -> list[str]:
    builder = _COMMAND_BUILDERS.get(request.action)
    if builder is None:
        raise ValueError(f"Peekaboo does not implement {request.action.value}")
    return [*builder(request, screenshot_path), "--json"]


def _capture_command(request: ComputerUseRequest, screenshot_path: Path | None) -> list[str]:
    command = ["see", *_target_args(request, snapshot=False, window_index=False)]
    if screenshot_path is not None:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--path", str(screenshot_path)])
    return command


def _list_apps_command(_request: ComputerUseRequest, _path: Path | None) -> list[str]:
    return ["list", "apps"]


def _list_windows_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    if request.app is None and request.pid is None:
        raise ValueError("Peekaboo list_windows requires app or pid")
    return ["list", "windows", *_app_target_args(request)]


def _launch_app_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    command = ["app", "launch"]
    if request.app is not None:
        command.append(request.app)
    _append_option(command, "--bundle-id", request.bundle_id)
    for url in request.urls:
        command.extend(["--open", url])
    _append_flag(command, "--wait-until-ready", enabled=request.wait_until_ready)
    _append_flag(command, "--no-focus", enabled=request.no_focus)
    return command


def _click_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    command = ["click"]
    if request.element is not None:
        command.extend(["--on", str(request.element)])
    elif request.query is not None:
        command.append(request.query)
    else:
        coordinate = cast("tuple[int, int]", request.coordinate)
        command.extend(["--coords", f"{coordinate[0]},{coordinate[1]}"])
    command.extend(_target_args(request))
    _append_flag(command, "--double", enabled=request.action is ComputerUseAction.DOUBLE_CLICK)
    _append_flag(command, "--right", enabled=request.action is ComputerUseAction.RIGHT_CLICK)
    _append_flag(
        command,
        "--foreground",
        enabled=request.foreground or request.action is ComputerUseAction.DOUBLE_CLICK,
    )
    return command


def _type_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    command = ["type", "--text", request.text or "", *_target_args(request)]
    _append_flag(command, "--clear", enabled=request.clear)
    _append_flag(command, "--foreground", enabled=request.foreground)
    return command


def _key_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    command = ["hotkey", "--keys", ",".join(_keys(request)), *_target_args(request)]
    _append_flag(command, "--foreground", enabled=request.foreground)
    return command


def _scroll_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    direction, amount = _scroll(request)
    command = ["scroll", "--direction", direction, "--amount", str(amount)]
    _append_option(command, "--on", request.element)
    command.extend(_target_args(request))
    _append_flag(command, "--smooth", enabled=request.smooth)
    return command


def _set_value_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    return [
        "set-value",
        "--value",
        request.value or "",
        "--on",
        str(request.element if request.element is not None else request.query),
        *_snapshot_args(request),
    ]


def _perform_action_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    return [
        "perform-action",
        "--on",
        str(request.element if request.element is not None else request.query),
        "--action",
        request.accessibility_action or "",
        *_snapshot_args(request),
    ]


def _menu_list_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    command = ["menu", "list", *_target_args(request, snapshot=False)]
    _append_flag(command, "--include-disabled", enabled=request.include_disabled)
    return command


def _menu_click_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    command = ["menu", "click", *_target_args(request, snapshot=False)]
    _append_option(command, "--path", request.menu_path)
    _append_option(command, "--item", request.menu_item)
    return command


def _permissions_command(_request: ComputerUseRequest, _path: Path | None) -> list[str]:
    return ["permissions", "status"]


def _dialog_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    command = [
        "dialog",
        request.action.value.removeprefix("dialog_"),
        *_target_args(request, snapshot=False),
    ]
    for flag, value in (
        ("--button", request.button),
        ("--text", request.text),
        ("--field", request.field),
        ("--index", request.index),
        ("--path", request.path),
        ("--name", request.name),
        ("--select", request.select),
    ):
        _append_option(command, flag, value)
    _append_flag(command, "--clear", enabled=request.clear)
    _append_flag(command, "--ensure-expanded", enabled=request.ensure_expanded)
    _append_flag(command, "--force", enabled=request.force)
    return command


def _clipboard_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    command = ["clipboard", request.action.value.removeprefix("clipboard_")]
    _append_option(command, "--text", request.text)
    _append_option(command, "--prefer", request.prefer)
    _append_option(command, "--slot", request.slot)
    _append_flag(command, "--verify", enabled=request.verify)
    return command


def _space_command(request: ComputerUseRequest, _path: Path | None) -> list[str]:
    subcommand = request.action.value.removeprefix("space_").replace("_", "-")
    command = ["space", subcommand]
    if request.action is ComputerUseAction.SPACE_LIST:
        _append_flag(command, "--detailed", enabled=request.detailed)
    elif request.action is ComputerUseAction.SPACE_SWITCH:
        _append_option(command, "--to", request.space)
    else:
        command.extend(_target_args(request, snapshot=False))
        _append_option(command, "--to", request.space)
        _append_flag(command, "--to-current", enabled=request.to_current)
        _append_flag(command, "--follow", enabled=request.follow)
    return command


def _target_args(
    request: ComputerUseRequest,
    *,
    snapshot: bool = True,
    window_index: bool = True,
) -> list[str]:
    args = _app_target_args(request)
    _append_option(args, "--window-id", request.window_id)
    _append_option(args, "--window-title", request.window_title)
    if window_index:
        _append_option(args, "--window-index", request.window_index)
    if snapshot:
        args.extend(_snapshot_args(request))
    return args


def _app_target_args(request: ComputerUseRequest) -> list[str]:
    args: list[str] = []
    _append_option(args, "--app", request.app)
    _append_option(args, "--pid", request.pid)
    return args


def _snapshot_args(request: ComputerUseRequest) -> list[str]:
    args: list[str] = []
    _append_option(args, "--snapshot", request.snapshot_id)
    return args


def _append_option(command: list[str], flag: str, value: object | None) -> None:
    if value is not None:
        command.extend([flag, str(value)])


def _append_flag(command: list[str], flag: str, *, enabled: bool) -> None:
    if enabled:
        command.append(flag)


def _keys(request: ComputerUseRequest) -> tuple[str, ...]:
    if isinstance(request.keys, str):
        return tuple(part.strip().lower() for part in request.keys.split("+") if part.strip())
    return tuple(part.lower() for part in request.keys or ())


def _scroll(request: ComputerUseRequest) -> tuple[str, int]:
    if request.direction is not None:
        return request.direction, request.amount or 3
    delta = request.delta_y or 0
    return ("down" if delta < 0 else "up"), max(1, abs(delta) // 120)


async def _run_json_command(command: list[str], *, timeout_seconds: float) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_seconds)
    except TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"Peekaboo timed out after {timeout_seconds:g}s") from exc
    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        error = stderr.decode(errors="replace").strip() or output or f"exit code {proc.returncode}"
        raise RuntimeError(f"Peekaboo failed: {error}")
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Peekaboo returned invalid JSON: {output or '<empty>'}") from exc
    if not isinstance(parsed, dict):
        raise TypeError("Peekaboo returned a non-object JSON response")
    if parsed.get("success") is False:
        response_error = parsed.get("error")
        if isinstance(response_error, dict):
            response_error = response_error.get("message") or response_error.get("code")
        raise RuntimeError(f"Peekaboo failed: {response_error or 'unknown error'}")
    return parsed


_COMMAND_BUILDERS: dict[
    ComputerUseAction,
    Callable[[ComputerUseRequest, Path | None], list[str]],
] = {
    ComputerUseAction.CAPTURE: _capture_command,
    ComputerUseAction.LIST_APPS: _list_apps_command,
    ComputerUseAction.LIST_WINDOWS: _list_windows_command,
    ComputerUseAction.LAUNCH_APP: _launch_app_command,
    **dict.fromkeys(_CLICK_ACTIONS, _click_command),
    ComputerUseAction.TYPE: _type_command,
    ComputerUseAction.KEY: _key_command,
    ComputerUseAction.SCROLL: _scroll_command,
    ComputerUseAction.SET_VALUE: _set_value_command,
    ComputerUseAction.PERFORM_ACTION: _perform_action_command,
    ComputerUseAction.MENU_LIST: _menu_list_command,
    ComputerUseAction.MENU_CLICK: _menu_click_command,
    ComputerUseAction.DIALOG_LIST: _dialog_command,
    ComputerUseAction.DIALOG_CLICK: _dialog_command,
    ComputerUseAction.DIALOG_INPUT: _dialog_command,
    ComputerUseAction.DIALOG_FILE: _dialog_command,
    ComputerUseAction.DIALOG_DISMISS: _dialog_command,
    ComputerUseAction.CLIPBOARD_GET: _clipboard_command,
    ComputerUseAction.CLIPBOARD_SET: _clipboard_command,
    ComputerUseAction.CLIPBOARD_CLEAR: _clipboard_command,
    ComputerUseAction.CLIPBOARD_SAVE: _clipboard_command,
    ComputerUseAction.CLIPBOARD_RESTORE: _clipboard_command,
    ComputerUseAction.SPACE_LIST: _space_command,
    ComputerUseAction.SPACE_SWITCH: _space_command,
    ComputerUseAction.SPACE_MOVE_WINDOW: _space_command,
    ComputerUseAction.CHECK_PERMISSIONS: _permissions_command,
}
