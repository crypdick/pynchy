"""Tests for the desktop screenshot service tool."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.config.models import AgentConfig
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations.desktop_screenshot import DesktopScreenshotPlugin


class _FakeProcess:
    def __init__(self, *, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr


def _handler(tool_name: str = "take_screenshot"):
    action = DesktopScreenshotPlugin().pynchy_service_handler().action_for(tool_name)
    assert action is not None
    return action.handler


def test_desktop_screenshot_plugin_is_registered() -> None:
    with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
        pm = get_plugin_manager()

    assert "builtin-desktop-screenshot" in [pm.get_name(p) for p in pm.get_plugins()]
    registration = pm.get_plugin("builtin-desktop-screenshot").pynchy_service_handler()
    assert registration.action_for("take_screenshot") is not None
    assert registration.action_for("analyze_screenshot") is not None


@pytest.mark.asyncio
async def test_take_screenshot_rejects_non_macos(tmp_path: Path) -> None:
    handler = _handler()
    settings = make_settings(data_dir=tmp_path)

    with (
        patch("pynchy.plugins.integrations.desktop_screenshot.get_settings", return_value=settings),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Linux"
        ),
    ):
        result = await handler({"source_group": "admin"})

    assert result == {"error": "Desktop screenshots are only supported on macOS hosts."}


@pytest.mark.action("desktop.screenshot.capture")
@pytest.mark.asyncio
async def test_take_screenshot_runs_screencapture_into_workspace_ipc(tmp_path: Path) -> None:
    handler = _handler()
    settings = make_settings(data_dir=tmp_path)
    captured_args: tuple[str, ...] | None = None

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        await asyncio.to_thread(Path(args[-1]).write_bytes, b"png bytes")
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
    settings = make_settings(data_dir=tmp_path)
    captured_args: tuple[str, ...] | None = None

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        await asyncio.to_thread(Path(args[-1]).write_bytes, b"png bytes")
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
    settings = make_settings(data_dir=tmp_path)

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


@pytest.mark.action("desktop.screenshot.analyze")
@pytest.mark.asyncio
async def test_analyze_screenshot_calls_gateway_with_workspace_image(tmp_path: Path) -> None:
    handler = _handler("analyze_screenshot")
    settings = make_settings(data_dir=tmp_path, agent=AgentConfig(model="gpt-5.5"))
    screenshot = tmp_path / "ipc" / "admin" / "screenshots" / "screen.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png bytes")

    with (
        patch("pynchy.plugins.integrations.desktop_screenshot.get_settings", return_value=settings),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot._request_vision_analysis",
            new=AsyncMock(return_value="The screen shows a terminal."),
        ) as request_vision,
    ):
        result = await handler(
            {
                "source_group": "admin",
                "image_path": "/workspace/ipc/screenshots/screen.png",
                "prompt": "What changed?",
            }
        )

    request_vision.assert_awaited_once()
    assert result == {
        "result": {
            "analysis": "The screen shows a terminal.",
            "container_path": "/workspace/ipc/screenshots/screen.png",
            "format": "png",
            "model": "gpt-5.5",
        }
    }


@pytest.mark.asyncio
async def test_analyze_screenshot_rejects_image_path_outside_workspace(tmp_path: Path) -> None:
    handler = _handler("analyze_screenshot")
    settings = make_settings(data_dir=tmp_path, agent=AgentConfig(model="gpt-5.5"))
    outside = tmp_path / "ipc" / "other" / "screenshots" / "screen.png"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"png bytes")

    with (
        patch("pynchy.plugins.integrations.desktop_screenshot.get_settings", return_value=settings),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot._request_vision_analysis",
            new=AsyncMock(),
        ),
    ):
        result = await handler({"source_group": "admin", "image_path": str(outside)})

    assert result == {
        "error": "Screenshot path must stay inside this workspace screenshots directory."
    }


@pytest.mark.asyncio
async def test_analyze_screenshot_defaults_to_latest_workspace_png(tmp_path: Path) -> None:
    handler = _handler("analyze_screenshot")
    settings = make_settings(data_dir=tmp_path, agent=AgentConfig(model="gpt-5.5"))
    screenshot_dir = tmp_path / "ipc" / "admin" / "screenshots"
    screenshot_dir.mkdir(parents=True)
    old = screenshot_dir / "20260709T010000Z-old.png"
    latest = screenshot_dir / "20260709T020000Z-latest.png"
    old.write_bytes(b"old")
    latest.write_bytes(b"latest")

    with (
        patch("pynchy.plugins.integrations.desktop_screenshot.get_settings", return_value=settings),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot._request_vision_analysis",
            new=AsyncMock(return_value="The screen shows the latest capture."),
        ) as request_vision,
    ):
        result = await handler({"source_group": "admin"})

    request_vision.assert_awaited_once()
    body = request_vision.await_args.args[0]
    content = body["input"][0]["content"]
    assert content[0] == {
        "type": "input_text",
        "text": (
            "Analyze this desktop screenshot. Describe the visible UI state, read any "
            "important text, and call out actionable details."
        ),
    }
    assert content[1] == {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{base64.b64encode(b'latest').decode()}",
    }
    assert result == {
        "result": {
            "analysis": "The screen shows the latest capture.",
            "container_path": "/workspace/ipc/screenshots/20260709T020000Z-latest.png",
            "format": "png",
            "model": "gpt-5.5",
        }
    }
