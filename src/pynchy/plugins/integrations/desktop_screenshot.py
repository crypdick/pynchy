"""Host-side desktop screenshot tool for macOS."""

from __future__ import annotations

import asyncio
import base64
import platform
import re
import subprocess  # noqa: S404 - uses PIPE constants with fixed screencapture argv.
from collections.abc import (
    Callable,
)
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pluggy

from pynchy.actions.api import ActionId
from pynchy.host.paths import PYNCHY_IPC_CONTAINER_PATH
from pynchy.plugins.api import (
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    HostActionAccess,
    HostActionDescriptor,
    HostActionHandler,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
)

hookimpl = pluggy.HookimplMarker("pynchy")

_SCREENSHOT_BIN = "/usr/sbin/screencapture"
_CONTAINER_SCREENSHOT_DIR = f"{PYNCHY_IPC_CONTAINER_PATH}/screenshots"
_VALID_MODES = {"full", "selection", "window"}
_ScreenshotRequest = tuple[str, Path, list[str]]
_DEFAULT_ANALYSIS_PROMPT = (
    "Analyze this desktop screenshot. Describe the visible UI state, read any important text, "
    "and call out actionable details."
)
_DEFAULT_MAX_OUTPUT_TOKENS = 1200
_DISPLAY_ID_ERROR = "display_id must be a positive integer"
_NO_SCREENSHOTS_ERROR = "No screenshots found for this workspace."
_SCREENSHOT_PATH_ERROR = "Screenshot path must stay inside this workspace screenshots directory."
_PNG_ONLY_ERROR = "Only PNG screenshots can be analyzed."
_SCREENSHOT_NOT_FOUND_ERROR = "Screenshot not found: {name}"
_MAX_OUTPUT_TOKENS_ERROR = "max_output_tokens must be a positive integer"
_VISION_RESPONSE_ERROR = "Vision response did not include text output."
_GATEWAY_NOT_RUNNING_ERROR = "LLM gateway is not running."
_LIFECYCLE_CONFIGURATION_ERROR = "Desktop screenshots require lifecycle configuration."


@dataclass(frozen=True)
class DesktopVisionGateway:
    """Connection coordinates for the local vision gateway."""

    port: int
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class DesktopScreenshotRuntime:
    """Resolved host values and gateway access supplied during startup."""

    data_dir: Path
    default_model: str
    vision_gateway: Callable[[], DesktopVisionGateway | None]


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
        raise ValueError(_DISPLAY_ID_ERROR)
    return raw


def _screenshot_path(*, data_dir: Path, source_group: str, label: object) -> Path:
    filename = f"{_timestamp()}-{_slug(label)}.png"
    return data_dir / "ipc" / source_group / "screenshots" / filename


def _screenshot_dir(*, data_dir: Path, source_group: str) -> Path:
    return data_dir / "ipc" / source_group / "screenshots"


def _path_is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _latest_screenshot(base_dir: Path) -> Path:
    screenshots = sorted(base_dir.glob("*.png"), key=lambda path: path.name)
    if not screenshots:
        raise ValueError(_NO_SCREENSHOTS_ERROR)
    return screenshots[-1]


def _resolve_screenshot_path(
    *,
    data_dir: Path,
    source_group: str,
    image_path: object,
) -> Path:
    base_dir = _screenshot_dir(data_dir=data_dir, source_group=source_group)
    if not isinstance(image_path, str) or not image_path.strip():
        candidate = _latest_screenshot(base_dir)
    elif image_path.startswith(f"{_CONTAINER_SCREENSHOT_DIR}/"):
        candidate = base_dir / Path(image_path).name
    else:
        raw = Path(image_path)
        candidate = raw if raw.is_absolute() else base_dir / raw.name

    resolved_base = base_dir.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if not _path_is_relative_to(resolved_candidate, resolved_base):
        raise ValueError(_SCREENSHOT_PATH_ERROR)
    if resolved_candidate.suffix.lower() != ".png":
        raise ValueError(_PNG_ONLY_ERROR)
    if not resolved_candidate.exists():
        raise ValueError(_SCREENSHOT_NOT_FOUND_ERROR.format(name=resolved_candidate.name))
    return resolved_candidate


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


def _prompt(data: dict[str, Any]) -> str:
    prompt = data.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return _DEFAULT_ANALYSIS_PROMPT


def _model(data: dict[str, Any], default_model: str) -> str:
    model = data.get("model")
    if isinstance(model, str) and model.strip():
        return model
    return default_model


def _max_output_tokens(data: dict[str, Any]) -> int:
    raw = data.get("max_output_tokens", _DEFAULT_MAX_OUTPUT_TOKENS)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(_MAX_OUTPUT_TOKENS_ERROR)
    return raw


def _build_vision_request(
    *,
    image_bytes: bytes,
    prompt: str,
    model: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    encoded = base64.b64encode(image_bytes).decode()
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"},
                ],
            }
        ],
        "max_output_tokens": max_output_tokens,
    }


def _response_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    chunks: list[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            chunks.extend(
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
    if chunks:
        return "\n".join(chunks)
    raise RuntimeError(_VISION_RESPONSE_ERROR)


async def _request_vision_analysis(
    body: dict[str, Any],
    runtime: DesktopScreenshotRuntime,
) -> str:
    gateway_instance = runtime.vision_gateway()
    if gateway_instance is None:
        raise RuntimeError(_GATEWAY_NOT_RUNNING_ERROR)

    url = f"http://localhost:{gateway_instance.port}/v1/responses"
    headers = {
        "Authorization": f"Bearer {gateway_instance.api_key}",
        "Content-Type": "application/json",
    }
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session,
        session.post(url, headers=headers, json=body) as response,
    ):
        response.raise_for_status()
        return _response_text(await response.json())


async def _handle_take_screenshot(
    data: dict[str, Any],
    runtime: DesktopScreenshotRuntime | None,
) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"error": "Desktop screenshots are only supported on macOS hosts."}
    if runtime is None:
        return {"error": _LIFECYCLE_CONFIGURATION_ERROR}

    request = _screenshot_request(data, runtime.data_dir)
    if isinstance(request, dict):
        return request
    mode, output_path, command = request

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


def _screenshot_request(
    data: dict[str, Any],
    data_dir: Path,
) -> _ScreenshotRequest | dict[str, str]:
    source_group = _source_group(data)
    if source_group is None:
        return {"error": "Missing or invalid source group for screenshot request."}

    mode = _mode(data)
    if mode is None:
        return {"error": 'mode must be one of "full", "selection", or "window".'}

    try:
        output_path = _screenshot_path(
            data_dir=data_dir,
            source_group=source_group,
            label=data.get("label"),
        )
        command = _command(data, output_path, mode)
    except ValueError as exc:
        return {"error": str(exc)}
    return (mode, output_path, command)


async def _handle_analyze_screenshot(
    data: dict[str, Any],
    runtime: DesktopScreenshotRuntime | None,
) -> dict[str, Any]:
    source_group = _source_group(data)
    if source_group is None:
        return {"error": "Missing or invalid source group for screenshot analysis request."}
    if runtime is None:
        return {"error": _LIFECYCLE_CONFIGURATION_ERROR}

    try:
        screenshot_path = _resolve_screenshot_path(
            data_dir=runtime.data_dir,
            source_group=source_group,
            image_path=data.get("image_path"),
        )
        model = _model(data, runtime.default_model)
        body = _build_vision_request(
            image_bytes=screenshot_path.read_bytes(),
            prompt=_prompt(data),
            model=model,
            max_output_tokens=_max_output_tokens(data),
        )
        analysis = await _request_vision_analysis(body, runtime)
    except ValueError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"Failed to read screenshot: {exc}"}
    except (aiohttp.ClientError, RuntimeError) as exc:
        return {"error": f"Vision analysis failed: {exc}"}

    return {
        "result": {
            "analysis": analysis,
            "container_path": f"{_CONTAINER_SCREENSHOT_DIR}/{screenshot_path.name}",
            "format": "png",
            "model": model,
        }
    }


def _screenshot_action(
    tool_name: str,
    action_id: str,
    summary: str,
    handler: HostActionHandler,
) -> HostActionDescriptor:
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId(action_id),
            kind=CapabilityKind.HOST_ACTION,
            owner="desktop-screenshot",
            summary=summary,
            action_ids=(ActionId(action_id),),
            documentation="docs/usage/host-capabilities/desktop-screenshots.md",
        ),
        tool_name=HostToolName(tool_name),
        handler=handler,
        # Both operations acquire workspace-scoped data; neither mutates an
        # external provider or public sink.
        access=HostActionAccess.READ,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(IdempotencyMode.NOT_REQUIRED),
        audit=AuditContract(),
    )


def _desktop_screenshot_actions(
    runtime: DesktopScreenshotRuntime | None,
) -> HostActionRegistration:
    async def take_screenshot(data: dict[str, Any]) -> dict[str, Any]:
        return await _handle_take_screenshot(data, runtime)

    async def analyze_screenshot(data: dict[str, Any]) -> dict[str, Any]:
        return await _handle_analyze_screenshot(data, runtime)

    return HostActionRegistration(
        actions=(
            _screenshot_action(
                "take_screenshot",
                "desktop.screenshot.capture",
                "Capture the host desktop into this workspace's screenshot directory.",
                take_screenshot,
            ),
            _screenshot_action(
                "analyze_screenshot",
                "desktop.screenshot.analyze",
                "Analyze one workspace desktop screenshot with the configured vision model.",
                analyze_screenshot,
            ),
        )
    )


class DesktopScreenshotPlugin:
    """Expose a macOS desktop screenshot service tool."""

    def __init__(self, runtime: DesktopScreenshotRuntime | None = None) -> None:
        self._runtime = runtime

    def configure(self, runtime: DesktopScreenshotRuntime) -> None:
        """Apply resolved host configuration before host-action registration."""
        self._runtime = runtime

    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return _desktop_screenshot_actions(self._runtime)
