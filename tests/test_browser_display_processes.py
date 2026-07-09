from __future__ import annotations

import subprocess  # noqa: S404, RUF100 - test double subclasses Popen; no subprocess launch.
from typing import TYPE_CHECKING, Any

from pynchy.plugins.integrations import browser
from pynchy.plugins.integrations.x_integration import (
    _display as x_display,  # allow: private-test-imports - process side effect.
)

if TYPE_CHECKING:
    import pytest


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


def test_x_display_uses_resolved_executables(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    paths = {
        "Xvfb": "/opt/bin/Xvfb",
        "x11vnc": "/opt/bin/x11vnc",
        "websockify": "/opt/bin/websockify",
    }

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        commands.append(command)
        return _FakeProcess(command, **kwargs)

    monkeypatch.setattr(x_display.shutil, "which", paths.__getitem__)
    monkeypatch.setattr(x_display, "has_display", lambda: False)
    monkeypatch.setattr(x_display.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(x_display.time, "sleep", lambda _seconds: None)
    x_display._state.xvfb_proc = None

    try:
        x_display.ensure_xvfb()
        x_display.start_vnc_layer()
    finally:
        x_display._state.xvfb_proc = None

    assert [command[0] for command in commands] == [
        "/opt/bin/Xvfb",
        "/opt/bin/x11vnc",
        "/opt/bin/websockify",
    ]
