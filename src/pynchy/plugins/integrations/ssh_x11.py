"""Computer-use provider for a real X11 desktop reached over SSH."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import shutil
import subprocess  # noqa: S404 - fixed SSH argv, request travels over stdin.
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import pluggy
from pydantic import BaseModel, Field

from pynchy.plugins.api import (
    ComputerUseBackend,
    ComputerUseBackendAvailability,
    ComputerUseRequest,
)
from pynchy.plugins.computer_use.artifacts import screenshot_artifact

hookimpl = pluggy.HookimplMarker("pynchy")
PositiveTimeout = Annotated[float, Field(gt=0)]


class SshX11Config(BaseModel):
    """SSH endpoint and pinned credential for a remote X11 helper."""

    model_config = {"extra": "forbid"}

    host: str = ""
    user: str = ""
    binary: Annotated[str, Field(min_length=1)] = "ssh"
    private_key: Path = Path("/srv/pynchy/app/data/personalization/ssh/x11_ed25519")
    known_hosts: Path = Path("/srv/pynchy/app/data/personalization/ssh/known_hosts")
    remote_command: Annotated[str, Field(min_length=1)] = "pynchy-x11-computer-use"
    timeout_seconds: PositiveTimeout = 30.0


@dataclass(frozen=True)
class SshX11Backend:
    """Send neutral computer-use requests to one pinned tailnet desktop."""

    config: SshX11Config

    @property
    def name(self) -> str:
        return "ssh-x11"

    def availability(self) -> ComputerUseBackendAvailability:
        if not self.config.host:
            return ComputerUseBackendAvailability(
                available=False, reason="SSH X11 host is not configured"
            )
        if shutil.which(self.config.binary) is None:
            return ComputerUseBackendAvailability(
                available=False,
                reason=f"SSH is not installed at {self.config.binary!r}",
            )
        for label, path in (
            ("private key", self.config.private_key),
            ("known-hosts file", self.config.known_hosts),
        ):
            if not path.is_file():
                return ComputerUseBackendAvailability(
                    available=False,
                    reason=f"SSH {label} is missing at {path}",
                )
        return ComputerUseBackendAvailability(available=True)

    async def execute(
        self,
        request: ComputerUseRequest,
        *,
        screenshot_path: Path | None = None,
    ) -> dict[str, Any]:
        target = f"{self.config.user}@{self.config.host}" if self.config.user else self.config.host
        command = [
            self.config.binary,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.config.known_hosts}",
            "-i",
            str(self.config.private_key),
            target,
            self.config.remote_command,
        ]
        payload = request.model_dump_json(exclude_none=True).encode()
        output = await _run_ssh(command, payload, self.config.timeout_seconds)
        screenshot = output.pop("screenshot_png_base64", None)
        if screenshot_path is not None:
            if not isinstance(screenshot, str):
                raise RuntimeError("SSH X11 helper did not return a screenshot")
            try:
                screenshot_bytes = base64.b64decode(screenshot, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise RuntimeError("SSH X11 helper returned invalid screenshot data") from exc
            await asyncio.to_thread(screenshot_path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(screenshot_path.write_bytes, screenshot_bytes)
        result: dict[str, Any] = {"backend": self.name, "output": output}
        if screenshot_path is not None:
            result["screenshot"] = await screenshot_artifact(screenshot_path)
        return result


class SshX11ComputerUsePlugin:
    """Contribute a remote real-desktop X11 computer-use provider."""

    def __init__(self, config: SshX11Config | None = None) -> None:
        self._config = config or SshX11Config()

    def configure(self, config: SshX11Config) -> None:
        self._config = config

    @hookimpl
    def pynchy_computer_use_backend(self) -> ComputerUseBackend:
        return SshX11Backend(self._config)


async def _run_ssh(command: list[str], payload: bytes, timeout_seconds: float) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload), timeout=timeout_seconds
        )
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise RuntimeError(f"SSH X11 request timed out after {timeout_seconds:g}s") from exc
    if process.returncode:
        error = stderr.decode(errors="replace").strip() or "unknown error"
        raise RuntimeError(f"SSH X11 request failed: {error}")
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("SSH X11 helper returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise TypeError("SSH X11 helper returned a non-object response")
    if remote_error := parsed.get("error"):
        raise RuntimeError(f"SSH X11 helper failed: {remote_error}")
    return parsed
