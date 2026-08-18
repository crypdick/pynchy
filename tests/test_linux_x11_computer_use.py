"""Contract tests for the isolated Linux X11 computer-use provider."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess  # noqa: S404 - tests construct inert CompletedProcess results.
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins.api import ComputerUseRequest
from pynchy.plugins.integrations.linux_x11 import (
    LinuxX11Backend,
    LinuxX11ComputerUsePlugin,
    LinuxX11Config,
)
from pynchy.plugins.integrations.ssh_x11_helper import PROTOCOL_VERSION, SUPPORTED_ACTIONS

if TYPE_CHECKING:
    from pathlib import Path


class _FakeProcess:
    def __init__(self, response: object) -> None:
        self.returncode = 0
        self.stdout = json.dumps(response).encode()
        self.stderr = b""
        self.input = b""

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        self.input = payload or b""
        return self.stdout, self.stderr

    def kill(self) -> None:
        pass


class _TimeoutProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__({})
        self.calls = 0
        self.killed = False

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(1)
        return await super().communicate(payload)

    def kill(self) -> None:
        self.killed = True


def _config(tmp_path: Path) -> LinuxX11Config:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.touch()
    return LinuxX11Config(binary="/usr/bin/kubectl", kubeconfig=kubeconfig)


def _handshake() -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            {
                "protocol_version": PROTOCOL_VERSION,
                "supported_actions": sorted(SUPPORTED_ACTIONS),
                "ready": True,
            }
        ).encode(),
        stderr=b"",
    )


def test_linux_x11_reports_ready_through_fixed_desktop_deployment(tmp_path: Path) -> None:
    backend = LinuxX11Backend(_config(tmp_path))

    with (
        patch(
            "pynchy.plugins.integrations.linux_x11.shutil.which",
            return_value="/usr/bin/kubectl",
        ),
        patch(
            "pynchy.plugins.integrations.linux_x11.subprocess.run",
            return_value=_handshake(),
        ) as run,
    ):
        status = backend.availability()

    assert status.available is True
    assert run.call_args.args[0] == [
        "/usr/bin/kubectl",
        "--kubeconfig",
        str(tmp_path / "kubeconfig"),
        "-n",
        "pynchy",
        "exec",
        "-i",
        "deployment/pynchy-desktop",
        "-c",
        "desktop",
        "--",
        "pynchy-x11-computer-use",
    ]
    assert json.loads(run.call_args.kwargs["input"])["action"] == "check_permissions"


def test_linux_x11_reports_missing_local_prerequisites(tmp_path: Path) -> None:
    missing_binary = LinuxX11Backend(_config(tmp_path))
    missing_config = LinuxX11Backend(
        LinuxX11Config(binary="/usr/bin/kubectl", kubeconfig=tmp_path / "missing")
    )

    with patch("pynchy.plugins.integrations.linux_x11.shutil.which", return_value=None):
        binary_status = missing_binary.availability()
    with patch(
        "pynchy.plugins.integrations.linux_x11.shutil.which",
        return_value="/usr/bin/kubectl",
    ):
        config_status = missing_config.availability()

    assert binary_status.reason == "kubectl is not installed at '/usr/bin/kubectl'"
    assert "Kubernetes config is missing" in (config_status.reason or "")


def test_linux_x11_rejects_unready_invalid_or_mismatched_helper(tmp_path: Path) -> None:
    backend = LinuxX11Backend(_config(tmp_path))
    responses = (
        ({"ready": True}, "protocol_version"),
        (
            {
                "protocol_version": PROTOCOL_VERSION + 1,
                "supported_actions": [],
                "ready": True,
            },
            "protocol mismatch",
        ),
        (
            {"protocol_version": PROTOCOL_VERSION, "supported_actions": [], "ready": False},
            "not ready",
        ),
    )

    for response, reason in responses:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(response).encode(), stderr=b""
        )
        with (
            patch(
                "pynchy.plugins.integrations.linux_x11.shutil.which",
                return_value="/usr/bin/kubectl",
            ),
            patch("pynchy.plugins.integrations.linux_x11.subprocess.run", return_value=completed),
        ):
            status = backend.availability()
        assert reason in (status.reason or "")


def test_linux_x11_projects_blocking_timeout(tmp_path: Path) -> None:
    backend = LinuxX11Backend(_config(tmp_path))

    with (
        patch(
            "pynchy.plugins.integrations.linux_x11.shutil.which",
            return_value="/usr/bin/kubectl",
        ),
        patch(
            "pynchy.plugins.integrations.linux_x11.subprocess.run",
            side_effect=subprocess.TimeoutExpired("kubectl", 30),
        ),
    ):
        status = backend.availability()

    assert "timed out" in (status.reason or "")


async def test_linux_x11_materializes_capture_and_names_cluster_target(tmp_path: Path) -> None:
    screenshot = b"png bytes"
    process = _FakeProcess(
        {
            "window": {"title": "myEDD - Chromium"},
            "screenshot_png_base64": base64.b64encode(screenshot).decode(),
        }
    )
    backend = LinuxX11Backend(_config(tmp_path))
    artifact = tmp_path / "capture.png"
    request = ComputerUseRequest(source_group="unemployment", action="capture", app="Chromium")

    with patch(
        "pynchy.plugins.integrations.linux_x11.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        result = await backend.execute(request, screenshot_path=artifact)

    assert artifact.read_bytes() == screenshot
    assert json.loads(process.input)["source_group"] == "unemployment"
    assert result["backend"] == "linux-x11"
    assert result["target"] == "deployment/pynchy-desktop"


def test_linux_x11_plugin_applies_configuration(tmp_path: Path) -> None:
    plugin = LinuxX11ComputerUsePlugin()
    config = _config(tmp_path)

    plugin.configure(config)

    assert plugin.pynchy_computer_use_backend().config is config


async def test_linux_x11_kills_timed_out_transport(tmp_path: Path) -> None:
    process = _TimeoutProcess()
    config = _config(tmp_path).model_copy(update={"timeout_seconds": 0.001})
    backend = LinuxX11Backend(config)
    request = ComputerUseRequest(source_group="unemployment", action="list_apps")

    with (
        patch(
            "pynchy.plugins.integrations.linux_x11.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        await backend.execute(request)

    assert process.killed is True
