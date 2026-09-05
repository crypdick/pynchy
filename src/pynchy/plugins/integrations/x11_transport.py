"""Shared response handling for X11 computer-use transports."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from pathlib import Path  # noqa: TC003 - beartype resolves runtime annotations.
from typing import Any

from pydantic import BaseModel

from pynchy.plugins.computer_use.artifacts import screenshot_artifact


class Handshake(BaseModel):
    """Versioned readiness response from the packaged X11 helper."""

    model_config = {"extra": "ignore"}

    protocol_version: int
    supported_actions: frozenset[str]  # noqa: V107
    ready: bool


def parse_response(
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    *,
    transport: str,
) -> dict[str, Any]:
    """Parse one closed helper response and preserve transport context."""
    parsed: object | None = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if not returncode:
                raise RuntimeError(f"{transport} X11 helper returned invalid JSON") from exc
    if isinstance(parsed, dict) and (remote_error := parsed.get("error")):
        raise RuntimeError(f"{transport} X11 helper failed: {remote_error}")
    if returncode:
        error = stderr.decode(errors="replace").strip() or "unknown error"
        raise RuntimeError(f"{transport} X11 request failed: {error}")
    if not isinstance(parsed, dict):
        raise TypeError(f"{transport} X11 helper returned a non-object response")
    return parsed


async def materialize_result(
    output: dict[str, Any],
    *,
    backend: str,
    transport: str,
    target: str | None = None,
    screenshot_path: Path | None = None,
) -> dict[str, Any]:
    """Write optional screenshot data and return the backend-neutral result."""
    screenshot = output.pop("screenshot_png_base64", None)
    if screenshot_path is not None:
        if not isinstance(screenshot, str):
            raise RuntimeError(f"{transport} helper did not return a screenshot")
        try:
            screenshot_bytes = base64.b64decode(screenshot, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(f"{transport} helper returned invalid screenshot data") from exc
        await asyncio.to_thread(screenshot_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(screenshot_path.write_bytes, screenshot_bytes)
    result: dict[str, Any] = {"backend": backend, "output": output}
    if target is not None:
        result["target"] = target
    if screenshot_path is not None:
        result["screenshot"] = await screenshot_artifact(screenshot_path)
    return result
