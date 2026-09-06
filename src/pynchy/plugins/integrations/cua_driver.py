"""Cua Driver provider plugin for macOS computer use."""

from __future__ import annotations

import asyncio
import json
import platform
import shutil
import subprocess  # noqa: S404 - resolved Cua binary runs with closed argv.
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import pluggy
from pydantic import BaseModel, Field

from pynchy.plugins.api import (
    ComputerUseAction,
    ComputerUseBackend,
    ComputerUseBackendAvailability,
    ComputerUseRequest,
)
from pynchy.plugins.computer_use.artifacts import screenshot_artifact

hookimpl = pluggy.HookimplMarker("pynchy")
PositiveTimeout = Annotated[float, Field(gt=0)]
_CUA_ACTIONS = frozenset(
    {
        ComputerUseAction.CAPTURE,
        ComputerUseAction.LIST_APPS,
        ComputerUseAction.LIST_WINDOWS,
        ComputerUseAction.LAUNCH_APP,
        ComputerUseAction.CLICK,
        ComputerUseAction.DOUBLE_CLICK,
        ComputerUseAction.RIGHT_CLICK,
        ComputerUseAction.TYPE,
        ComputerUseAction.KEY,
        ComputerUseAction.SCROLL,
        ComputerUseAction.CHECK_PERMISSIONS,
    }
)
_CLICK_ACTIONS = frozenset(
    {
        ComputerUseAction.CLICK,
        ComputerUseAction.DOUBLE_CLICK,
        ComputerUseAction.RIGHT_CLICK,
    }
)


class CuaDriverConfig(BaseModel):
    """Plugin-owned Cua Driver executable and timeout configuration."""

    model_config = {"extra": "forbid"}

    # NOTE: Update docs/usage/host-capabilities/computer-use.md § Built-in: Cua Driver
    # if these defaults change.
    binary: Annotated[str, Field(min_length=1)] = "cua-driver"
    timeout_seconds: PositiveTimeout = 30.0


@dataclass(frozen=True)
class CuaDriverBackend:
    """Translate neutral requests to the Cua Driver transport."""

    config: CuaDriverConfig

    @property
    def name(self) -> str:
        return "cua-driver"

    def availability(self) -> ComputerUseBackendAvailability:
        if platform.system() != "Darwin":
            return ComputerUseBackendAvailability(
                available=False,
                reason="Cua Driver requires macOS",
            )
        if shutil.which(self.config.binary) is None:
            return ComputerUseBackendAvailability(
                available=False,
                reason=f"Cua Driver is not installed at {self.config.binary!r}",
            )
        return ComputerUseBackendAvailability(available=True)

    async def execute(
        self,
        request: ComputerUseRequest,
        *,
        screenshot_path: Path | None = None,
    ) -> dict[str, Any]:
        if request.action not in _CUA_ACTIONS:
            raise ValueError(
                f"{request.action.value} requires another computer-use provider; "
                "Cua Driver does not support it"
            )
        binary = shutil.which(self.config.binary)
        if binary is None:
            raise RuntimeError(f"Cua Driver is not installed at {self.config.binary!r}")
        action, payload = _action_and_payload(request)
        command = [binary, "call", action]
        if payload:
            command.append(json.dumps(payload, separators=(",", ":")))
        if screenshot_path is not None:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            command.extend(["--screenshot-out-file", str(screenshot_path)])
        output = await _run_command(
            command,
            action=action,
            timeout_seconds=self.config.timeout_seconds,
        )
        result: dict[str, Any] = {
            "backend": self.name,
            "cua_action": action,
            "output": output,
        }
        if screenshot_path is not None:
            result["screenshot"] = await screenshot_artifact(screenshot_path)
        return result


class CuaDriverComputerUsePlugin:
    """Contribute Cua Driver as an optional macOS provider."""

    def __init__(self, config: CuaDriverConfig | None = None) -> None:
        self._config = config or CuaDriverConfig()

    def configure(self, config: CuaDriverConfig) -> None:
        """Apply the provider's resolved service configuration before registration."""
        self._config = config

    @hookimpl
    def pynchy_computer_use_backend(self) -> ComputerUseBackend:
        return CuaDriverBackend(self._config)


def _action_and_payload(request: ComputerUseRequest) -> tuple[str, dict[str, Any]]:
    action = request.action
    payload = _base_payload(request)
    if action is ComputerUseAction.CAPTURE:
        return "get_window_state", _window_payload(request)
    if action is ComputerUseAction.TYPE:
        payload["text"] = request.text
        return "type_text", payload
    if action is ComputerUseAction.KEY:
        payload["keys"] = list(_keys(request))
        return "hotkey", payload
    if action is ComputerUseAction.SCROLL:
        _add_scroll_payload(payload, request)
        return "scroll", payload
    if action in _CLICK_ACTIONS:
        return action.value, _click_payload(payload, request)
    return action.value, payload


def _window_payload(request: ComputerUseRequest) -> dict[str, Any]:
    if request.pid is None:
        raise ValueError("Cua Driver capture requires pid")
    payload: dict[str, Any] = {"pid": request.pid}
    _put(payload, "window_id", request.window_id)
    _put(payload, "query", request.query)
    return payload


def _click_payload(payload: dict[str, Any], request: ComputerUseRequest) -> dict[str, Any]:
    if isinstance(request.element, str):
        raise TypeError("stable element references require another computer-use provider")
    if request.element is not None:
        payload["element_index"] = request.element
    if request.coordinate is not None:
        payload["x"], payload["y"] = request.coordinate
    if "element_index" not in payload and "x" not in payload:
        raise ValueError("Cua Driver click actions require numeric element or coordinate")
    return payload


def _add_scroll_payload(payload: dict[str, Any], request: ComputerUseRequest) -> None:
    if request.delta_y is not None:
        payload["delta_y"] = request.delta_y
        return
    amount = (request.amount or 3) * 120
    if request.direction == "down":
        payload["delta_y"] = -amount
    elif request.direction == "up":
        payload["delta_y"] = amount
    elif request.direction == "left":
        payload["delta_x"] = -amount
    else:
        payload["delta_x"] = amount


def _base_payload(request: ComputerUseRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in ("app", "pid", "window_id", "bundle_id", "urls", "query"):
        value = getattr(request, field)
        if value not in (None, ()):
            payload[field] = list(value) if isinstance(value, tuple) else value
    return payload


def _put(payload: dict[str, Any], key: str, value: object | None) -> None:
    if value is not None:
        payload[key] = value


def _keys(request: ComputerUseRequest) -> tuple[str, ...]:
    if isinstance(request.keys, str):
        return tuple(part.strip().lower() for part in request.keys.split("+") if part.strip())
    return tuple(part.lower() for part in request.keys or ())


async def _run_command(
    command: list[str],
    *,
    action: str,
    timeout_seconds: float,
) -> str:
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
        raise RuntimeError(f"cua-driver {action} timed out after {timeout_seconds:g}s") from exc
    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        error = stderr.decode(errors="replace").strip() or output or f"exit code {proc.returncode}"
        raise RuntimeError(f"cua-driver {action} failed: {error}")
    return output
