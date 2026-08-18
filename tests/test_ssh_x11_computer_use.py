"""Contract tests for the SSH X11 computer-use provider."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins.api import ComputerUseRequest
from pynchy.plugins.integrations.ssh_x11 import SshX11Backend, SshX11Config

if TYPE_CHECKING:
    from pathlib import Path


class _FakeProcess:
    def __init__(
        self,
        response: object = None,
        *,
        returncode: int = 0,
        stderr: bytes = b"",
        stdout: bytes | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = json.dumps(response).encode() if stdout is None else stdout
        self.stderr = stderr
        self.input = b""
        self.killed = False

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        self.input = payload or b""
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True


class _TimeoutProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__({})
        self.calls = 0

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(1)
        return await super().communicate(payload)


def _config(tmp_path: Path) -> SshX11Config:
    key = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    key.touch()
    known_hosts.touch()
    return SshX11Config(
        host="100.72.183.9",
        user="operator",
        binary="/usr/bin/ssh",
        private_key=key,
        known_hosts=known_hosts,
    )


def test_ssh_x11_requires_pinned_connection_files(tmp_path) -> None:
    backend = SshX11Backend(SshX11Config(host="desktop", private_key=tmp_path / "missing-key"))

    with patch("pynchy.plugins.integrations.ssh_x11.shutil.which", return_value="/usr/bin/ssh"):
        status = backend.availability()

    assert status.available is False
    assert "private key is missing" in (status.reason or "")


@pytest.mark.parametrize(
    ("config", "binary", "reason"),
    [
        (SshX11Config(), "/usr/bin/ssh", "host is not configured"),
        (SshX11Config(host="desktop"), None, "SSH is not installed"),
    ],
)
def test_ssh_x11_reports_unavailable_local_prerequisites(
    config: SshX11Config,
    binary: str | None,
    reason: str,
) -> None:
    with patch("pynchy.plugins.integrations.ssh_x11.shutil.which", return_value=binary):
        status = SshX11Backend(config).availability()

    assert status.available is False
    assert reason in (status.reason or "")


def test_ssh_x11_reports_ready_with_pinned_files(tmp_path) -> None:
    backend = SshX11Backend(_config(tmp_path))

    with patch("pynchy.plugins.integrations.ssh_x11.shutil.which", return_value="/usr/bin/ssh"):
        status = backend.availability()

    assert status.available is True


@pytest.mark.asyncio
async def test_ssh_x11_sends_request_over_stdin_and_materializes_capture(tmp_path) -> None:
    screenshot = b"png bytes"
    process = _FakeProcess(
        {
            "window": {"title": "myEDD - Brave"},
            "screenshot_png_base64": base64.b64encode(screenshot).decode(),
        }
    )
    backend = SshX11Backend(_config(tmp_path))
    artifact = tmp_path / "capture.png"
    request = ComputerUseRequest(
        source_group="unemployment",
        action="capture",
        app="Brave",
    )

    with patch(
        "pynchy.plugins.integrations.ssh_x11.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ) as create:
        result = await backend.execute(request, screenshot_path=artifact)

    assert artifact.read_bytes() == screenshot
    assert json.loads(process.input)["source_group"] == "unemployment"
    assert create.call_args.args[-2:] == ("operator@100.72.183.9", "pynchy-x11-computer-use")
    assert result["backend"] == "ssh-x11"
    assert result["screenshot"]["bytes"] == len(screenshot)


@pytest.mark.asyncio
async def test_ssh_x11_rejects_invalid_screenshot_data(tmp_path) -> None:
    backend = SshX11Backend(_config(tmp_path))
    request = ComputerUseRequest(source_group="unemployment", action="capture")

    with (
        patch(
            "pynchy.plugins.integrations.ssh_x11.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess({"screenshot_png_base64": "!"})),
        ),
        pytest.raises(RuntimeError, match="invalid screenshot data"),
    ):
        await backend.execute(request, screenshot_path=tmp_path / "capture.png")


@pytest.mark.asyncio
async def test_ssh_x11_requires_screenshot_in_capture_response(tmp_path) -> None:
    backend = SshX11Backend(_config(tmp_path))
    request = ComputerUseRequest(source_group="unemployment", action="capture")

    with (
        patch(
            "pynchy.plugins.integrations.ssh_x11.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess({})),
        ),
        pytest.raises(RuntimeError, match="did not return a screenshot"),
    ):
        await backend.execute(request, screenshot_path=tmp_path / "capture.png")


@pytest.mark.asyncio
async def test_ssh_x11_returns_non_capture_output_without_artifact(tmp_path) -> None:
    config = _config(tmp_path).model_copy(update={"user": ""})
    backend = SshX11Backend(config)
    request = ComputerUseRequest(source_group="unemployment", action="list_apps")

    with patch(
        "pynchy.plugins.integrations.ssh_x11.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_FakeProcess({"apps": ["Brave"]})),
    ) as create:
        result = await backend.execute(request)

    assert create.call_args.args[-2:] == ("100.72.183.9", "pynchy-x11-computer-use")
    assert result == {"backend": "ssh-x11", "output": {"apps": ["Brave"]}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process", "message"),
    [
        (_FakeProcess(returncode=1, stderr=b"connection denied"), "connection denied"),
        (_FakeProcess(returncode=1), "unknown error"),
        (_FakeProcess(stdout=b"not json"), "invalid JSON"),
        (_FakeProcess(["not", "an", "object"]), "non-object response"),
        (_FakeProcess({"error": "desktop denied"}), "desktop denied"),
    ],
)
async def test_ssh_x11_projects_transport_failures(
    tmp_path,
    process: _FakeProcess,
    message: str,
) -> None:
    backend = SshX11Backend(_config(tmp_path))
    request = ComputerUseRequest(source_group="unemployment", action="list_apps")

    with (
        patch(
            "pynchy.plugins.integrations.ssh_x11.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
        pytest.raises((RuntimeError, TypeError), match=message),
    ):
        await backend.execute(request)


@pytest.mark.asyncio
async def test_ssh_x11_kills_timed_out_transport(tmp_path) -> None:
    process = _TimeoutProcess()
    config = _config(tmp_path).model_copy(update={"timeout_seconds": 0.001})
    backend = SshX11Backend(config)
    request = ComputerUseRequest(source_group="unemployment", action="list_apps")

    with (
        patch(
            "pynchy.plugins.integrations.ssh_x11.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        await backend.execute(request)

    assert process.killed is True
