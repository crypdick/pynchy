"""Tests for the host-side computer_use service tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations.computer_use import ComputerUsePlugin


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"ok",
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _handler():
    return ComputerUsePlugin().pynchy_service_handler()["tools"]["computer_use"]


def test_computer_use_plugin_is_registered() -> None:
    with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
        pm = get_plugin_manager()

    assert "builtin-computer-use" in [pm.get_name(p) for p in pm.get_plugins()]
    handlers = pm.get_plugin("builtin-computer-use").pynchy_service_handler()
    assert "computer_use" in handlers["tools"]


@pytest.mark.asyncio
async def test_computer_use_rejects_non_macos(tmp_path: Path) -> None:
    handler = _handler()

    with (
        patch(
            "pynchy.plugins.integrations.computer_use.platform.system",
            return_value="Linux",
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.get_settings",
            return_value=SimpleNamespace(data_dir=tmp_path),
        ),
    ):
        result = await handler({"source_group": "admin", "action": "list_apps"})

    assert result == {"error": "computer_use is only supported on macOS hosts."}


@pytest.mark.asyncio
async def test_computer_use_reports_missing_cua_driver(tmp_path: Path) -> None:
    handler = _handler()

    with (
        patch(
            "pynchy.plugins.integrations.computer_use.platform.system",
            return_value="Darwin",
        ),
        patch("pynchy.plugins.integrations.computer_use.shutil.which", return_value=None),
        patch(
            "pynchy.plugins.integrations.computer_use.get_settings",
            return_value=SimpleNamespace(data_dir=tmp_path),
        ),
    ):
        result = await handler({"source_group": "admin", "action": "list_apps"})

    assert result == {
        "error": "cua-driver is not installed on the host; install Cua Driver before using computer_use."
    }


@pytest.mark.asyncio
async def test_capture_runs_get_window_state_with_screenshot_artifact(tmp_path: Path) -> None:
    handler = _handler()
    captured_args: tuple[str, ...] | None = None

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        screenshot_path = Path(args[-1])
        screenshot_path.write_bytes(b"png bytes")
        return _FakeProcess(stdout=b"window state")

    with (
        patch(
            "pynchy.plugins.integrations.computer_use.platform.system",
            return_value="Darwin",
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.shutil.which", return_value="/bin/cua-driver"
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.get_settings",
            return_value=SimpleNamespace(data_dir=tmp_path),
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
        patch(
            "pynchy.plugins.integrations.computer_use._timestamp",
            return_value="20260709T120000Z",
        ),
    ):
        result = await handler(
            {
                "source_group": "admin",
                "action": "capture",
                "pid": 844,
                "window_id": 10725,
                "label": "Calculator",
            }
        )

    host_path = tmp_path / "ipc" / "admin" / "computer-use" / "20260709T120000Z-calculator.png"
    assert captured_args == (
        "/bin/cua-driver",
        "call",
        "get_window_state",
        '{"pid":844,"window_id":10725}',
        "--screenshot-out-file",
        str(host_path),
    )
    assert result == {
        "result": {
            "action": "capture",
            "cua_action": "get_window_state",
            "output": "window state",
            "screenshot": {
                "host_path": str(host_path),
                "container_path": "/workspace/ipc/computer-use/20260709T120000Z-calculator.png",
                "format": "png",
                "bytes": len(b"png bytes"),
            },
        }
    }


@pytest.mark.asyncio
async def test_click_maps_element_index_and_capture_after(tmp_path: Path) -> None:
    handler = _handler()
    captured_calls: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        captured_calls.append(args)
        if "--screenshot-out-file" in args:
            Path(args[-1]).write_bytes(b"after png")
            return _FakeProcess(stdout=b"after state")
        return _FakeProcess(stdout=b"clicked")

    with (
        patch(
            "pynchy.plugins.integrations.computer_use.platform.system",
            return_value="Darwin",
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.shutil.which", return_value="/bin/cua-driver"
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.get_settings",
            return_value=SimpleNamespace(data_dir=tmp_path),
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
        patch(
            "pynchy.plugins.integrations.computer_use._timestamp",
            return_value="20260709T120000Z",
        ),
    ):
        result = await handler(
            {
                "source_group": "admin",
                "action": "click",
                "pid": 844,
                "window_id": 10725,
                "element": 14,
                "capture_after": True,
            }
        )

    host_path = tmp_path / "ipc" / "admin" / "computer-use" / "20260709T120000Z-after-click.png"
    assert captured_calls == [
        (
            "/bin/cua-driver",
            "call",
            "click",
            '{"pid":844,"window_id":10725,"element_index":14}',
        ),
        (
            "/bin/cua-driver",
            "call",
            "get_window_state",
            '{"pid":844,"window_id":10725}',
            "--screenshot-out-file",
            str(host_path),
        ),
    ]
    assert result["result"]["output"] == "clicked"
    assert result["result"]["after"]["output"] == "after state"
    assert result["result"]["after"]["screenshot"]["bytes"] == len(b"after png")


@pytest.mark.asyncio
async def test_key_splits_shortcut_into_hotkey_payload(tmp_path: Path) -> None:
    handler = _handler()
    captured_args: tuple[str, ...] | None = None

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        return _FakeProcess(stdout=b"sent")

    with (
        patch(
            "pynchy.plugins.integrations.computer_use.platform.system",
            return_value="Darwin",
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.shutil.which", return_value="/bin/cua-driver"
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.get_settings",
            return_value=SimpleNamespace(data_dir=tmp_path),
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
    ):
        result = await handler(
            {"source_group": "admin", "action": "key", "pid": 844, "keys": "cmd+s"}
        )

    assert captured_args == (
        "/bin/cua-driver",
        "call",
        "hotkey",
        '{"pid":844,"keys":["cmd","s"]}',
    )
    assert result["result"]["output"] == "sent"


@pytest.mark.asyncio
async def test_command_failure_is_returned(tmp_path: Path) -> None:
    handler = _handler()

    with (
        patch(
            "pynchy.plugins.integrations.computer_use.platform.system",
            return_value="Darwin",
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.shutil.which", return_value="/bin/cua-driver"
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.get_settings",
            return_value=SimpleNamespace(data_dir=tmp_path),
        ),
        patch(
            "pynchy.plugins.integrations.computer_use.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess(returncode=1, stderr=b"permission denied")),
        ),
    ):
        result = await handler({"source_group": "admin", "action": "list_apps"})

    assert result == {"error": "cua-driver list_apps failed: permission denied"}
