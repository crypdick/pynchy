from __future__ import annotations

import asyncio
import subprocess  # noqa: S404 - test double subclasses Popen; no subprocess launch.
from typing import Any
from unittest.mock import MagicMock

import pytest

from pynchy.plugins.integrations import browser
from pynchy.plugins.integrations.x_integration import XIntegrationPlugin


class _FakeProcess(subprocess.Popen[bytes]):
    returncode = None

    def __init__(self, command: list[str], **_kwargs: Any) -> None:
        self.command = command

    def poll(self) -> int | None:
        return self.returncode


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


def test_browser_reports_how_to_install_chrome_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(browser, "_detect_chrome", lambda: None)

    with pytest.raises(RuntimeError, match="Install Google Chrome"):
        browser.chrome_path()


def test_browser_start_reports_missing_headless_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser,
        "resolve_executables",
        lambda *_names: (_ for _ in ()).throw(RuntimeError("Xvfb")),
    )

    with pytest.raises(RuntimeError, match="Headless display requires: Xvfb"):
        browser.start_virtual_display()


def test_browser_rejects_a_missing_vnc_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "_is_process_running", lambda _name: False)
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="x11vnc"):
        browser.ensure_vnc_stack_alive()


def test_browser_profile_uses_configured_project_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("PYNCHY_PROJECT_ROOT", str(tmp_path))

    profile = browser.profile_dir("google")

    assert profile == tmp_path / "data" / "playwright-profiles" / "google"
    assert profile.is_dir()


def test_browser_detects_a_live_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":42")
    monkeypatch.setattr(browser.shutil, "which", lambda _name: "/usr/bin/xdpyinfo")
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )

    assert browser.has_display() is True
    assert browser.display_is_live(":99") is True


def test_browser_display_checks_fail_closed_without_display_or_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)

    assert browser.has_display() is False
    assert browser.display_is_live(":99") is False


def test_browser_stops_processes_and_kills_stragglers() -> None:
    waiting = _FakeProcess(["waiting"])
    waiting.terminate = MagicMock()
    waiting.kill = MagicMock()
    waiting.wait = MagicMock()
    waiting.wait.side_effect = [subprocess.TimeoutExpired("wait", 5), None]
    stuck = _FakeProcess(["stuck"])
    stuck.returncode = 0

    browser.stop_procs([waiting, stuck])

    waiting.terminate.assert_called_once_with()
    waiting.kill.assert_called_once_with()
    waiting.wait.assert_called_with(timeout=2)


def test_browser_reuses_live_virtual_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        browser,
        "resolve_executables",
        lambda *_names: {"Xvfb": "xvfb", "x11vnc": "x11vnc", "websockify": "websockify"},
    )
    monkeypatch.setattr(browser, "display_is_live", lambda _display: True)
    monkeypatch.setattr(browser, "ensure_vnc_stack_alive", list)
    monkeypatch.setattr(browser, "_resolve_novnc_url", lambda: "http://host:6080/vnc.html")

    procs, url = browser.start_virtual_display()

    assert procs == []
    assert url == "http://host:6080/vnc.html"
    assert browser.os.environ["DISPLAY"] == ":99"


def test_browser_removes_stale_profile_locks(tmp_path) -> None:
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (tmp_path / name).touch()

    browser.cleanup_lock_files(tmp_path)

    assert not list(tmp_path.iterdir())


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
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._actions.has_display", lambda: False
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._actions._run_x_session_setup",
        fake_run_session_setup,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._actions.stop_procs", lambda _procs: None
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.has_display", lambda: False
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display._state", MagicMock(xvfb_proc=None)
    )
    monkeypatch.setattr(browser.shutil, "which", paths.__getitem__)
    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)

    result = await setup_tool({"timeout_seconds": 1})

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
