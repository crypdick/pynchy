from __future__ import annotations

import asyncio
import subprocess  # noqa: S404 - test double subclasses Popen; no subprocess launch.
from typing import TYPE_CHECKING, Any

import pytest

from pynchy.plugins.integrations import browser
from pynchy.plugins.integrations.x_integration import XIntegrationPlugin

if TYPE_CHECKING:
    from collections.abc import Callable


class _FakeProcess(subprocess.Popen[bytes]):
    returncode = None

    def __init__(self, command: list[str], **_kwargs: Any) -> None:
        self.command = command

    def poll(self) -> None:
        return None


def test_browser_virtual_display_uses_resolved_executables(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    paths = {
        "Xvfb": "/opt/bin/Xvfb",
        "x11vnc": "/opt/bin/x11vnc",
        "websockify": "/opt/bin/websockify",
    }

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        commands.append(command)
        return _FakeProcess(command, **kwargs)

    monkeypatch.setattr(browser.shutil, "which", paths.__getitem__)
    monkeypatch.setattr(browser, "display_is_live", lambda _display: False)
    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)

    browser.start_virtual_display()

    assert [command[0] for command in commands] == [
        "/opt/bin/Xvfb",
        "/opt/bin/x11vnc",
        "/opt/bin/websockify",
    ]


def test_browser_vnc_repair_uses_resolved_executables(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    paths = {
        "x11vnc": "/opt/bin/x11vnc",
        "websockify": "/opt/bin/websockify",
    }

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        commands.append(command)
        return _FakeProcess(command, **kwargs)

    monkeypatch.setattr(browser.shutil, "which", paths.__getitem__)
    monkeypatch.setattr(browser, "_is_process_running", lambda _name: False)
    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)

    browser.ensure_vnc_stack_alive()

    assert [command[0] for command in commands] == ["/opt/bin/x11vnc", "/opt/bin/websockify"]


def test_browser_requires_an_existing_explicit_chrome_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    missing = tmp_path / "missing-chrome"
    monkeypatch.setenv("CHROME_PATH", str(missing))

    with pytest.raises(RuntimeError, match="does not exist"):
        browser.chrome_path()


def test_browser_prefers_explicit_chrome_path_over_system_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    chrome = tmp_path / "chrome"
    chrome.touch()
    monkeypatch.setenv("CHROME_PATH", str(chrome))
    monkeypatch.setattr(browser, "_detect_chrome", lambda: "/system/chrome")

    assert browser.chrome_path() == str(chrome)


@pytest.mark.action("social.x.session.setup")
async def test_x_session_setup_uses_resolved_display_executables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    paths = {
        "Xvfb": "/opt/bin/Xvfb",
        "x11vnc": "/opt/bin/x11vnc",
        "websockify": "/opt/bin/websockify",
    }

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        commands.append(command)
        return _FakeProcess(command, **kwargs)

    async def fake_run_session_setup(
        _timeout_seconds: int,
        novnc_url: str | None,
    ) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"result": {"status": "ok", "novnc_url": novnc_url}}

    action = XIntegrationPlugin().pynchy_service_handler().action_for("setup_x_session")
    assert action is not None
    setup_tool = action.handler
    setup_handler = getattr(
        setup_tool,
        "__wrapped__",
        setup_tool,
    )
    action_globals = setup_handler.__globals__
    ensure_xvfb: Callable[[], None] = action_globals["ensure_xvfb"]
    display_globals = getattr(ensure_xvfb, "__wrapped__", ensure_xvfb).__globals__

    monkeypatch.setitem(action_globals, "has_display", lambda: False)
    monkeypatch.setitem(action_globals, "_run_x_session_setup", fake_run_session_setup)
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)
    monkeypatch.setitem(display_globals, "has_display", lambda: False)
    monkeypatch.setattr(display_globals["shutil"], "which", paths.__getitem__)
    monkeypatch.setattr(display_globals["subprocess"], "Popen", fake_popen)
    monkeypatch.setattr(display_globals["time"], "sleep", lambda _seconds: None)
    display_globals["_state"].xvfb_proc = None

    try:
        result = await setup_tool({"timeout_seconds": 1})
    finally:
        display_globals["_state"].xvfb_proc = None

    assert result == {
        "result": {
            "status": "ok",
            "novnc_url": "http://HOST:6080/vnc.html?autoconnect=true",
        }
    }
    assert [command[0] for command in commands] == [
        "/opt/bin/Xvfb",
        "/opt/bin/x11vnc",
        "/opt/bin/websockify",
    ]
