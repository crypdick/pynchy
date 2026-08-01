"""Tests for the desktop screenshot service tool."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from conftest import make_settings

from pynchy.config.api import AgentConfig, Settings
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations.desktop_screenshot import (
    DesktopScreenshotPlugin,
    DesktopScreenshotRuntime,
    DesktopVisionGateway,
)


class _FakeProcess:
    def __init__(self, *, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr


class _FakeVisionResponse:
    def __init__(self, payload: dict[str, object], error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    async def __aenter__(self) -> _FakeVisionResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    async def json(self) -> dict[str, object]:
        return self._payload


class _FakeVisionSession:
    def __init__(self, response: _FakeVisionResponse) -> None:
        self.response = response
        self.url: str | None = None
        self.headers: dict[str, str] | None = None
        self.body: dict[str, object] | None = None

    async def __aenter__(self) -> _FakeVisionSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, object]
    ) -> _FakeVisionResponse:
        self.url = url
        self.headers = headers
        self.body = json
        return self.response


def _runtime(settings: Settings) -> DesktopScreenshotRuntime:
    return DesktopScreenshotRuntime(
        data_dir=settings.data_dir,
        default_model=settings.agent.model or "gpt-5.5",
        vision_gateway=lambda: DesktopVisionGateway(port=4000, api_key="test-key"),
    )


def _handler(settings: Settings, tool_name: str = "take_screenshot"):
    registration = DesktopScreenshotPlugin(_runtime(settings)).pynchy_service_handler()
    action = registration.action_for(tool_name)
    assert action is not None
    return action.handler


def test_desktop_screenshot_plugin_is_registered(tmp_path: Path) -> None:
    with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
        pm = get_plugin_manager()

    assert "builtin-desktop-screenshot" in [pm.get_name(p) for p in pm.get_plugins()]
    plugin = pm.get_plugin("builtin-desktop-screenshot")
    assert isinstance(plugin, DesktopScreenshotPlugin)
    plugin.configure(_runtime(make_settings(data_dir=tmp_path)))
    registration = plugin.pynchy_service_handler()
    assert registration.action_for("take_screenshot") is not None
    assert registration.action_for("analyze_screenshot") is not None


@pytest.mark.asyncio
async def test_take_screenshot_rejects_non_macos(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings)

    with patch(
        "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Linux"
    ):
        result = await handler({"source_group": "admin"})

    assert result == {"error": "Desktop screenshots are only supported on macOS hosts."}


@pytest.mark.action("desktop.screenshot.capture")
@pytest.mark.asyncio
async def test_take_screenshot_runs_screencapture_into_workspace_ipc(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings)
    captured_args: tuple[str, ...] | None = None

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        await asyncio.to_thread(Path(args[-1]).write_bytes, b"png bytes")
        return _FakeProcess()

    with (
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
            "container_path": "/run/pynchy/screenshots/20260709T120000Z-main-display.png",
            "format": "png",
            "mode": "full",
            "bytes": len(b"png bytes"),
        }
    }


@pytest.mark.asyncio
async def test_take_screenshot_supports_window_selection_and_display_id(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings)
    captured_args: tuple[str, ...] | None = None

    async def fake_exec(*args: str, **kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        await asyncio.to_thread(Path(args[-1]).write_bytes, b"png bytes")
        return _FakeProcess()

    with (
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
        "/run/pynchy/screenshots/20260709T120000Z-screenshot.png"
    )


@pytest.mark.asyncio
async def test_take_screenshot_supports_selection_mode(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings)
    captured_args: tuple[str, ...] | None = None

    async def fake_exec(*args: str, **_kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        await asyncio.to_thread(Path(args[-1]).write_bytes, b"png bytes")
        return _FakeProcess()

    with (
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Darwin"
        ),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
    ):
        result = await handler({"source_group": "admin", "mode": "selection"})

    assert captured_args is not None
    assert captured_args[4:6] == ("-i", "-s")
    assert result["result"]["mode"] == "selection"


@pytest.mark.asyncio
async def test_take_screenshot_returns_command_failure(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings)

    with (
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
    settings = make_settings(data_dir=tmp_path, agent=AgentConfig(model="gpt-5.5"))
    handler = _handler(settings, "analyze_screenshot")
    screenshot = tmp_path / "ipc" / "admin" / "screenshots" / "screen.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png bytes")

    with patch(
        "pynchy.plugins.integrations.desktop_screenshot._request_vision_analysis",
        new=AsyncMock(return_value="The screen shows a terminal."),
    ) as request_vision:
        result = await handler(
            {
                "source_group": "admin",
                "image_path": "/run/pynchy/screenshots/screen.png",
                "prompt": "What changed?",
            }
        )

    request_vision.assert_awaited_once()
    assert result == {
        "result": {
            "analysis": "The screen shows a terminal.",
            "container_path": "/run/pynchy/screenshots/screen.png",
            "format": "png",
            "model": "gpt-5.5",
        }
    }


@pytest.mark.asyncio
async def test_analyze_screenshot_rejects_image_path_outside_workspace(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path, agent=AgentConfig(model="gpt-5.5"))
    handler = _handler(settings, "analyze_screenshot")
    outside = tmp_path / "ipc" / "other" / "screenshots" / "screen.png"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"png bytes")

    with patch(
        "pynchy.plugins.integrations.desktop_screenshot._request_vision_analysis",
        new=AsyncMock(),
    ):
        result = await handler({"source_group": "admin", "image_path": str(outside)})

    assert result == {
        "error": "Screenshot path must stay inside this workspace screenshots directory."
    }


@pytest.mark.asyncio
async def test_analyze_screenshot_defaults_to_latest_workspace_png(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path, agent=AgentConfig(model="gpt-5.5"))
    handler = _handler(settings, "analyze_screenshot")
    screenshot_dir = tmp_path / "ipc" / "admin" / "screenshots"
    screenshot_dir.mkdir(parents=True)
    old = screenshot_dir / "20260709T010000Z-old.png"
    latest = screenshot_dir / "20260709T020000Z-latest.png"
    old.write_bytes(b"old")
    latest.write_bytes(b"latest")

    with patch(
        "pynchy.plugins.integrations.desktop_screenshot._request_vision_analysis",
        new=AsyncMock(return_value="The screen shows the latest capture."),
    ) as request_vision:
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
            "container_path": "/run/pynchy/screenshots/20260709T020000Z-latest.png",
            "format": "png",
            "model": "gpt-5.5",
        }
    }


@pytest.mark.asyncio
async def test_analyze_screenshot_requires_a_workspace_capture(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings, "analyze_screenshot")

    assert await handler({"source_group": "admin"}) == {
        "error": "No screenshots found for this workspace."
    }


@pytest.mark.asyncio
async def test_analyze_screenshot_uses_requested_model(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path, agent=AgentConfig(model="default-model"))
    handler = _handler(settings, "analyze_screenshot")
    screenshot = tmp_path / "ipc" / "admin" / "screenshots" / "screen.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png")

    with patch(
        "pynchy.plugins.integrations.desktop_screenshot._request_vision_analysis",
        new=AsyncMock(return_value="Requested model used."),
    ) as request_vision:
        result = await handler(
            {"source_group": "admin", "image_path": "screen.png", "model": "custom-model"}
        )

    assert result["result"]["model"] == "custom-model"
    assert request_vision.await_args.args[0]["model"] == "custom-model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_analysis"),
    [
        ({"output_text": "Direct response"}, "Direct response"),
        (
            {
                "output": [
                    "ignored",
                    {"content": "ignored"},
                    {"content": [{"text": "First"}, {"type": "image"}, {"text": "Second"}]},
                ]
            },
            "First\nSecond",
        ),
    ],
)
async def test_analyze_screenshot_accepts_supported_gateway_response_shapes(
    tmp_path: Path,
    payload: dict[str, object],
    expected_analysis: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(data_dir=tmp_path, agent=AgentConfig(model="vision-model"))
    handler = _handler(settings, "analyze_screenshot")
    screenshot = tmp_path / "ipc" / "admin" / "screenshots" / "screen.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png")
    fake_session = _FakeVisionSession(_FakeVisionResponse(payload))
    monkeypatch.setattr(
        "pynchy.plugins.integrations.desktop_screenshot.aiohttp.ClientSession",
        lambda **_kwargs: fake_session,
    )

    result = await handler({"source_group": "admin", "image_path": "screen.png"})

    assert result["result"]["analysis"] == expected_analysis
    assert fake_session.url == "http://localhost:4000/v1/responses"
    assert fake_session.headers == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
    }
    assert fake_session.body is not None
    assert fake_session.body["model"] == "vision-model"


@pytest.mark.asyncio
async def test_analyze_screenshot_reports_gateway_http_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings, "analyze_screenshot")
    screenshot = tmp_path / "ipc" / "admin" / "screenshots" / "screen.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png")
    fake_session = _FakeVisionSession(
        _FakeVisionResponse({}, aiohttp.ClientError("gateway rejected request"))
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.desktop_screenshot.aiohttp.ClientSession",
        lambda **_kwargs: fake_session,
    )

    result = await handler({"source_group": "admin", "image_path": "screen.png"})

    assert result == {"error": "Vision analysis failed: gateway rejected request"}


@pytest.mark.asyncio
async def test_analyze_screenshot_reports_response_without_text(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings, "analyze_screenshot")
    screenshot = tmp_path / "ipc" / "admin" / "screenshots" / "screen.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png")
    fake_session = _FakeVisionSession(_FakeVisionResponse({"output": "not-a-list"}))
    monkeypatch.setattr(
        "pynchy.plugins.integrations.desktop_screenshot.aiohttp.ClientSession",
        lambda **_kwargs: fake_session,
    )

    result = await handler({"source_group": "admin", "image_path": "screen.png"})

    assert result == {
        "error": "Vision analysis failed: Vision response did not include text output."
    }


@pytest.mark.asyncio
async def test_screenshot_actions_validate_platform_runtime_and_request(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    plugin = DesktopScreenshotPlugin()
    action = plugin.pynchy_service_handler().action_for("take_screenshot")
    assert action is not None
    handler = action.handler

    with patch(
        "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Darwin"
    ):
        assert await handler({"source_group": "admin"}) == {
            "error": "Desktop screenshots require lifecycle configuration."
        }

    handler = _handler(settings)
    with patch(
        "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Darwin"
    ):
        assert await handler({}) == {
            "error": "Missing or invalid source group for screenshot request."
        }
        assert await handler({"source_group": "../admin"}) == {
            "error": "Missing or invalid source group for screenshot request."
        }
        assert await handler({"source_group": "admin", "mode": "bogus"}) == {
            "error": 'mode must be one of "full", "selection", or "window".'
        }
        assert await handler({"source_group": "admin", "display_id": True}) == {
            "error": "display_id must be a positive integer"
        }


@pytest.mark.asyncio
async def test_take_screenshot_reports_missing_output_and_empty_stderr(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings)
    with (
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Darwin"
        ),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess(returncode=1)),
        ),
    ):
        result = await handler({"source_group": "admin"})
    assert result == {"error": "screencapture failed: exit code 1"}

    with (
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Darwin"
        ),
        patch(
            "pynchy.plugins.integrations.desktop_screenshot.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess()),
        ),
    ):
        result = await handler({"source_group": "admin"})
    assert result == {"error": "screencapture succeeded but did not create an output file."}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "Missing or invalid source group for screenshot analysis request."),
        (
            {"source_group": "admin", "max_output_tokens": 0},
            "max_output_tokens must be a positive integer",
        ),
        (
            {"source_group": "admin", "image_path": "latest.jpg"},
            "Only PNG screenshots can be analyzed.",
        ),
    ],
)
async def test_analyze_screenshot_rejects_invalid_requests(
    tmp_path: Path, data: dict[str, object], message: str
) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings, "analyze_screenshot")
    screenshot = tmp_path / "ipc" / "admin" / "screenshots" / "latest.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png")
    result = await handler(data)
    assert result == {"error": message}

    plugin = DesktopScreenshotPlugin()
    no_runtime_action = plugin.pynchy_service_handler().action_for("analyze_screenshot")
    assert no_runtime_action is not None
    no_runtime = no_runtime_action.handler
    with patch(
        "pynchy.plugins.integrations.desktop_screenshot.platform.system", return_value="Darwin"
    ):
        assert await no_runtime({"source_group": "admin"}) == {
            "error": "Desktop screenshots require lifecycle configuration."
        }


@pytest.mark.asyncio
async def test_analyze_screenshot_reports_missing_file_and_vision_failures(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    handler = _handler(settings, "analyze_screenshot")

    with patch(
        "pynchy.plugins.integrations.desktop_screenshot._request_vision_analysis",
        new=AsyncMock(side_effect=RuntimeError("gateway unavailable")),
    ):
        result = await handler({"source_group": "admin", "image_path": "missing.png"})
    assert result == {"error": "Screenshot not found: missing.png"}

    screenshot = tmp_path / "ipc" / "admin" / "screenshots" / "screen.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png")
    with patch(
        "pynchy.plugins.integrations.desktop_screenshot._request_vision_analysis",
        new=AsyncMock(side_effect=RuntimeError("gateway unavailable")),
    ):
        result = await handler({"source_group": "admin", "image_path": "screen.png"})
    assert result == {"error": "Vision analysis failed: gateway unavailable"}


@pytest.mark.asyncio
async def test_analyze_screenshot_handles_gateway_absence_and_read_errors(tmp_path: Path) -> None:
    settings = make_settings(data_dir=tmp_path)
    screenshot = tmp_path / "ipc" / "admin" / "screenshots" / "screen.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png")
    runtime = DesktopScreenshotRuntime(
        data_dir=tmp_path,
        default_model="gpt-test",
        vision_gateway=lambda: None,
    )
    action = (
        DesktopScreenshotPlugin(runtime).pynchy_service_handler().action_for("analyze_screenshot")
    )
    assert action is not None
    handler = action.handler
    assert await handler({"source_group": "admin", "image_path": "screen.png"}) == {
        "error": "Vision analysis failed: LLM gateway is not running."
    }

    with patch.object(Path, "read_bytes", side_effect=OSError("read failed")):
        result = await _handler(settings, "analyze_screenshot")(
            {"source_group": "admin", "image_path": "screen.png"}
        )
    assert result == {"error": "Failed to read screenshot: read failed"}
