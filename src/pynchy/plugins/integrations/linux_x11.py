"""Computer-use provider for the isolated Kubernetes X11 desktop."""

from __future__ import annotations

import asyncio
import shutil
import subprocess  # noqa: S404 - fixed kubectl argv, request travels over stdin.
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import pluggy
from pydantic import BaseModel, Field, ValidationError

from pynchy.plugins.api import (
    ComputerUseBackend,
    ComputerUseBackendAvailability,
    ComputerUseRequest,
)
from pynchy.plugins.integrations.ssh_x11_helper import PROTOCOL_VERSION
from pynchy.plugins.integrations.x11_transport import Handshake, materialize_result, parse_response

hookimpl = pluggy.HookimplMarker("pynchy")
PositiveTimeout = Annotated[float, Field(gt=0)]


class LinuxX11Config(BaseModel):
    """Pinned Kubernetes target for the isolated desktop."""

    model_config = {"extra": "forbid"}

    binary: Annotated[str, Field(min_length=1)] = "kubectl"
    kubeconfig: Path = Path("/run/pynchy/kubeconfig.json")
    namespace: Annotated[str, Field(min_length=1)] = "pynchy"
    deployment: Annotated[str, Field(min_length=1)] = "pynchy-desktop"
    container: Annotated[str, Field(min_length=1)] = "desktop"
    helper: Annotated[str, Field(min_length=1)] = "pynchy-x11-computer-use"
    timeout_seconds: PositiveTimeout = 30.0


@dataclass(frozen=True)
class LinuxX11Backend:
    """Send computer-use requests to one isolated in-cluster desktop."""

    config: LinuxX11Config

    @property
    def name(self) -> str:
        return "linux-x11"

    @property
    def target(self) -> str:
        return f"deployment/{self.config.deployment}"

    def availability(self) -> ComputerUseBackendAvailability:
        if shutil.which(self.config.binary) is None:
            return ComputerUseBackendAvailability(
                available=False,
                reason=f"kubectl is not installed at {self.config.binary!r}",
            )
        if not self.config.kubeconfig.is_file():
            return ComputerUseBackendAvailability(
                available=False,
                reason=f"Kubernetes config is missing at {self.config.kubeconfig}",
            )
        try:
            response = _run_blocking(
                _command(self.config),
                b'{"action":"check_permissions"}',
                self.config.timeout_seconds,
            )
            handshake = Handshake.model_validate(response)
        except (RuntimeError, TypeError, ValidationError) as exc:
            return ComputerUseBackendAvailability(available=False, reason=str(exc))
        if handshake.protocol_version != PROTOCOL_VERSION:
            return ComputerUseBackendAvailability(
                available=False,
                reason=(
                    f"Linux X11 protocol mismatch: host expects {PROTOCOL_VERSION}, "
                    f"helper returned {handshake.protocol_version}"
                ),
            )
        if not handshake.ready:
            return ComputerUseBackendAvailability(
                available=False,
                reason="Linux X11 helper reported not ready",
            )
        return ComputerUseBackendAvailability(available=True)

    async def execute(
        self,
        request: ComputerUseRequest,
        *,
        screenshot_path: Path | None = None,
    ) -> dict[str, Any]:
        output = await _run(
            _command(self.config),
            request.model_dump_json(exclude_none=True).encode(),
            self.config.timeout_seconds,
        )
        return await materialize_result(
            output,
            backend=self.name,
            transport="Linux X11",
            target=self.target,
            screenshot_path=screenshot_path,
        )


class LinuxX11ComputerUsePlugin:
    """Contribute an isolated Linux desktop computer-use provider."""

    def __init__(self, config: LinuxX11Config | None = None) -> None:
        self._config = config or LinuxX11Config()

    def configure(self, config: LinuxX11Config) -> None:
        self._config = config

    @hookimpl
    def pynchy_computer_use_backend(self) -> ComputerUseBackend:
        return LinuxX11Backend(self._config)


def _command(config: LinuxX11Config) -> list[str]:
    return [
        config.binary,
        "--kubeconfig",
        str(config.kubeconfig),
        "-n",
        config.namespace,
        "exec",
        "-i",
        f"deployment/{config.deployment}",
        "-c",
        config.container,
        "--",
        config.helper,
    ]


def _run_blocking(command: list[str], payload: bytes, timeout_seconds: float) -> dict[str, Any]:
    try:
        result = subprocess.run(  # noqa: S603 - fixed kubectl argv; request travels over stdin.
            command,
            input=payload,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Linux X11 request timed out after {timeout_seconds:g}s") from exc
    return parse_response(
        result.returncode,
        result.stdout,
        result.stderr,
        transport="Linux",
    )


async def _run(command: list[str], payload: bytes, timeout_seconds: float) -> dict[str, Any]:
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
        raise RuntimeError(f"Linux X11 request timed out after {timeout_seconds:g}s") from exc
    return parse_response(process.returncode or 0, stdout, stderr, transport="Linux")
