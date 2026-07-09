"""Tests for the desktop screenshot service tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations.desktop_screenshot import DesktopScreenshotPlugin


class _FakeProcess:
    def __init__(self, *, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr


def _handler():
    return DesktopScreenshotPlugin().pynchy_service_handler()["tools"]["take_screenshot"]


def test_desktop_screenshot_plugin_is_registered() -> None:
    with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
        pm = get_plugin_manager()

    assert "builtin-desktop-screenshot" in [pm.get_name(p) for p in pm.get_plugins()]
    handlers = pm.get_plugin("builtin-desktop-screenshot").pynchy_service_handler()
    assert "take_screenshot" in handlers["tools"]


@pytest.mark.asyncio
async def test_take_screenshot_rejects_non_macos(tmp_path: Path) -> None:
    handler = _handler()
    settings = SimpleNamespace(data_dir=tmp_path)

    with (
        patch("pynchy.plugins.integrations.desktop_screenshot.get_settings", return_value=settings),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Linux"
        ),
    ):
        result = await handler({"source_group": "admin"})

    assert result == {"error": "Desktop screenshots are only supported on macOS hosts."}


@pytest.mark.asyncio
async def test_take_screenshot_runs_screencapture_into_workspace_ipc(tmp_path: Path) -> None:
    handler = _handler()
    settings = SimpleNamespace(data_dir=tmp_path)
    captured_args: tuple[str, ...] | None = None

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        Path(args[-1]).write_bytes(b"png bytes")
        return _FakeProcess()

    with (
        patch("pynchy.plugins.integrations.desktop_screenshot.get_settings", return_value=settings),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Darwin"
        ),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot._timestamp",
            return_value="20260709T120000Z",
        ),
    ):
        result = await handler({"source_group": "admin", "label": "Main Display"})

    host_path = tmp_path / "ipc" / "admin" / "screenshots" / "20260709T120000Z-main-display.png"
    assert captured_args == ("/usr/sbin/screencapture", "-x", "-t", "png", str(host_path))
    assert result == {
        "result": {
            "host_path": str(host_path),
            "container_path": "/workspace/ipc/screenshots/20260709T120000Z-main-display.png",
            "format": "png",
            "mode": "full",
            "bytes": len(b"png bytes"),
        }
    }


@pytest.mark.asyncio
async def test_take_screenshot_supports_window_selection_and_display_id(tmp_path: Path) -> None:
    handler = _handler()
    settings = SimpleNamespace(data_dir=tmp_path)
    captured_args: tuple[str, ...] | None = None

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        Path(args[-1]).write_bytes(b"png bytes")
        return _FakeProcess()

    with (
        patch("pynchy.plugins.integrations.desktop_screenshot.get_settings", return_value=settings),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Darwin"
        ),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot._timestamp",
            return_value="20260709T120000Z",
        ),
    ):
        result = await handler(
            {
                "source_group": "admin",
                "mode": "window",
                "display_id": 2,
                "include_cursor": True,
            }
        )

    host_path = tmp_path / "ipc" / "admin" / "screenshots" / "20260709T120000Z-screenshot.png"
    assert captured_args == (
        "/usr/sbin/screencapture",
        "-x",
        "-t",
        "png",
        "-C",
        "-D",
        "2",
        "-i",
        "-w",
        str(host_path),
    )
    assert result["result"]["container_path"] == (
        "/workspace/ipc/screenshots/20260709T120000Z-screenshot.png"
    )


@pytest.mark.asyncio
async def test_take_screenshot_returns_command_failure(tmp_path: Path) -> None:
    handler = _handler()
    settings = SimpleNamespace(data_dir=tmp_path)

    with (
        patch("pynchy.plugins.integrations.desktop_screenshot.get_settings", return_value=settings),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Darwin"
        ),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess(returncode=1, stderr=b"permission denied")),
        ),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot._timestamp",
            return_value="20260709T120000Z",
        ),
    ):
        result = await handler({"source_group": "admin"})

    assert result == {"error": "screencapture failed: permission denied"}
