"""Computer-use provider for a real X11 desktop reached over SSH."""

from __future__ import annotations

import asyncio
import shutil
import subprocess  # noqa: S404 - fixed SSH argv, request travels over stdin.
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


class SshX11Config(BaseModel):
    """SSH endpoint and pinned credential for a remote X11 helper."""

    model_config = {"extra": "forbid"}

    host: str = ""
    user: str = ""
    binary: Annotated[str, Field(min_length=1)] = "ssh"
    private_key: Path = Path("/srv/pynchy/app/data/personalization/ssh/x11_ed25519")
    known_hosts: Path = Path("/srv/pynchy/app/data/personalization/ssh/known_hosts")
    timeout_seconds: PositiveTimeout = 30.0


@dataclass(frozen=True)
class SshX11Backend:
    """Send neutral computer-use requests to one pinned tailnet desktop."""

    config: SshX11Config

    @property
    def name(self) -> str:
        return "ssh-x11"

    def availability(self) -> ComputerUseBackendAvailability:
        if unavailable := _local_unavailability(self.config):
            return unavailable
        try:
            response = _run_ssh_blocking(
                _ssh_command(self.config),
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
                    f"SSH X11 protocol mismatch: host expects {PROTOCOL_VERSION}, "
                    f"helper returned {handshake.protocol_version}"
                ),
            )
        if not handshake.ready:
            return ComputerUseBackendAvailability(
                available=False,
                reason="SSH X11 helper reported not ready",
            )
        return ComputerUseBackendAvailability(available=True)

    async def execute(
        self,
        request: ComputerUseRequest,
        *,
        screenshot_path: Path | None = None,
    ) -> dict[str, Any]:
        payload = request.model_dump_json(exclude_none=True).encode()
        output = await _run_ssh(
            _ssh_command(self.config),
            payload,
            self.config.timeout_seconds,
        )
        return await materialize_result(
            output,
            backend=self.name,
            transport="SSH X11",
            screenshot_path=screenshot_path,
        )


class SshX11ComputerUsePlugin:
    """Contribute a remote real-desktop X11 computer-use provider."""

    def __init__(self, config: SshX11Config | None = None) -> None:
        self._config = config or SshX11Config()

    def configure(self, config: SshX11Config) -> None:
        self._config = config

    @hookimpl
    def pynchy_computer_use_backend(self) -> ComputerUseBackend:
        return SshX11Backend(self._config)


def _local_unavailability(config: SshX11Config) -> ComputerUseBackendAvailability | None:
    if not config.host:
        return ComputerUseBackendAvailability(
            available=False, reason="SSH X11 host is not configured"
        )
    if shutil.which(config.binary) is None:
        return ComputerUseBackendAvailability(
            available=False,
            reason=f"SSH is not installed at {config.binary!r}",
        )
    for label, path in (
        ("private key", config.private_key),
        ("known-hosts file", config.known_hosts),
    ):
        if not path.is_file():
            return ComputerUseBackendAvailability(
                available=False,
                reason=f"SSH {label} is missing at {path}",
            )
    return None


def _ssh_command(config: SshX11Config) -> list[str]:
    target = f"{config.user}@{config.host}" if config.user else config.host
    return [
        config.binary,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={config.known_hosts}",
        "-i",
        str(config.private_key),
        target,
    ]


def _parse_ssh_response(returncode: int, stdout: bytes, stderr: bytes) -> dict[str, Any]:
    return parse_response(returncode, stdout, stderr, transport="SSH")


def _run_ssh_blocking(command: list[str], payload: bytes, timeout_seconds: float) -> dict[str, Any]:
    try:
        result = subprocess.run(  # noqa: S603 - fixed SSH argv; request travels over stdin.
            command,
            input=payload,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"SSH X11 request timed out after {timeout_seconds:g}s") from exc
    return _parse_ssh_response(result.returncode, result.stdout, result.stderr)


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
    return _parse_ssh_response(process.returncode or 0, stdout, stderr)
