"""Public X-session behavior around display provisioning."""

from __future__ import annotations

import asyncio
import os
import subprocess  # noqa: S404 - tests construct timeout fixtures only.
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.x_integration import XIntegrationPlugin


def _handler() -> Any:
    action = XIntegrationPlugin().pynchy_service_handler().action_for("setup_x_session")
    assert action is not None
    return action.handler


def _action_globals(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    handler = _handler()
    current = handler
    while "ensure_xvfb" not in current.__globals__:
        current = current.__wrapped__
    return current.__globals__


@pytest.mark.asyncio
async def test_setup_session_reports_missing_xvfb(monkeypatch: pytest.MonkeyPatch) -> None:
    action_globals = _action_globals(monkeypatch)
    monkeypatch.setitem(action_globals, "has_display", lambda: False)
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.shutil.which",
        lambda _name: None,
    )

    result = await _handler()({})

    assert result == {
        "error": (
            "No display available and Xvfb not installed. X automation requires headed mode "
            "to avoid bot detection. Install with: apt install xvfb"
        )
    }


@pytest.mark.asyncio
async def test_setup_session_reports_missing_vnc_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_globals = _action_globals(monkeypatch)
    monkeypatch.setitem(action_globals, "has_display", lambda: False)
    monkeypatch.setitem(action_globals, "ensure_xvfb", lambda: None)
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.stop_procs",
        lambda _procs: None,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.shutil.which",
        lambda name: "/usr/bin/Xvfb" if name in {"Xvfb", "x11vnc"} else None,
    )

    result = await _handler()({})

    assert result == {
        "error": "VNC layer requires: websockify. Install with: apt install x11vnc novnc"
    }


@pytest.mark.asyncio
async def test_setup_session_uses_existing_xvfb_without_vnc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunningProcess:
        def poll(self) -> None:
            return None

    class DisplayState:
        xvfb_proc = RunningProcess()

    action_globals = _action_globals(monkeypatch)
    monkeypatch.setitem(action_globals, "has_display", lambda: True)
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)
    monkeypatch.setitem(
        action_globals,
        "_run_x_session_setup",
        AsyncMock(return_value={"result": {"status": "ok"}}),
    )
    monkeypatch.setattr("pynchy.plugins.integrations.x_integration._display._state", DisplayState())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.has_display",
        lambda: False,
    )
    monkeypatch.delenv("DISPLAY", raising=False)

    result = await _handler()({})

    assert result == {"result": {"status": "ok"}}
    assert os.environ["DISPLAY"] == ":99"


@pytest.mark.asyncio
async def test_setup_session_starts_xvfb_when_no_native_display_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunningProcess(subprocess.Popen[bytes]):
        returncode = None

        def __init__(self) -> None:
            self.returncode = None

        def poll(self) -> int | None:
            return self.returncode

    action_globals = _action_globals(monkeypatch)
    monkeypatch.setitem(action_globals, "has_display", lambda: True)
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)
    monkeypatch.setitem(
        action_globals,
        "_run_x_session_setup",
        AsyncMock(return_value={"result": {"status": "ok"}}),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display._state",
        type("DisplayState", (), {"xvfb_proc": None})(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.has_display",
        lambda: False,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.shutil.which",
        lambda _name: "/usr/bin/Xvfb",
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.subprocess.Popen",
        lambda _args, **_kwargs: RunningProcess(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.time.sleep",
        lambda _seconds: None,
    )
    monkeypatch.delenv("DISPLAY", raising=False)

    result = await _handler()({})

    assert result == {"result": {"status": "ok"}}
    assert os.environ["DISPLAY"] == ":99"


@pytest.mark.asyncio
async def test_setup_session_skips_xvfb_when_native_display_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_globals = _action_globals(monkeypatch)
    monkeypatch.setitem(action_globals, "has_display", lambda: True)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.has_display",
        lambda: True,
    )
    monkeypatch.setitem(
        action_globals,
        "_run_x_session_setup",
        AsyncMock(return_value={"result": {"status": "ok"}}),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("native display must not start Xvfb"),
    )

    assert await _handler()({}) == {"result": {"status": "ok"}}


@pytest.mark.asyncio
async def test_setup_session_reports_xvfb_exiting_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        returncode = 7

        def poll(self) -> int:
            return self.returncode

    action_globals = _action_globals(monkeypatch)
    monkeypatch.setitem(action_globals, "has_display", lambda: False)
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.shutil.which",
        lambda name: "/usr/bin/Xvfb" if name == "Xvfb" else None,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.subprocess.Popen",
        lambda *_args, **_kwargs: ExitedProcess(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.time.sleep",
        lambda _seconds: None,
    )

    assert await _handler()({}) == {"error": "Xvfb exited immediately (code 7)"}


def test_cleanup_xvfb_kills_a_process_that_ignores_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowProcess:
        def __init__(self) -> None:
            self.terminated = False
            self.killed = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: int) -> None:
            raise subprocess.TimeoutExpired("Xvfb", timeout)

        def kill(self) -> None:
            self.killed = True

    process = SlowProcess()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display._state",
        type("DisplayState", (), {"xvfb_proc": process})(),
    )

    ensure_xvfb = _action_globals(monkeypatch)["ensure_xvfb"]
    cleanup = ensure_xvfb.__wrapped__.__globals__["cleanup_xvfb"]
    cleanup()

    assert process.terminated is True
    assert process.killed is True
    cleanup()


@pytest.mark.asyncio
async def test_setup_session_reports_vnc_url_after_starting_both_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[list[str]] = []
    popen_type = subprocess.Popen

    def fake_popen(args: list[str], **_kwargs: object) -> subprocess.Popen[bytes]:
        launched.append(args)
        process = popen_type.__new__(popen_type)
        process._child_created = False  # noqa: V101
        process.returncode = None
        process.poll = lambda: None
        return process

    action_globals = _action_globals(monkeypatch)
    monkeypatch.setitem(action_globals, "has_display", lambda: False)
    monkeypatch.setitem(action_globals, "ensure_xvfb", lambda: None)
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)

    async def fake_setup(_timeout: object, novnc_url: str | None) -> dict[str, object]:
        await asyncio.sleep(0)
        return {"result": {"status": "ok", "novnc_url": novnc_url}}

    monkeypatch.setitem(
        action_globals,
        "_run_x_session_setup",
        fake_setup,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display._resolve_executables",
        lambda *_names: {"x11vnc": "/usr/bin/x11vnc", "websockify": "/usr/bin/websockify"},
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.subprocess.Popen",
        fake_popen,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.time.sleep",
        lambda _seconds: None,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.Path.is_dir",
        lambda _path: True,
    )

    result = await _handler()({})

    assert result == {
        "result": {
            "status": "ok",
            "novnc_url": "http://HOST:6080/vnc.html?autoconnect=true",
        }
    }
    assert launched == [
        [
            "/usr/bin/x11vnc",
            "-display",
            ":99",
            "-forever",
            "-nopw",
            "-rfbport",
            "5999",
            "-quiet",
        ],
        ["/usr/bin/websockify", "--web", "/usr/share/novnc", "6080", "localhost:5999"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returncodes", "message"),
    [
        ((1,), "x11vnc exited immediately (code 1)"),
        ((None, 2), "websockify exited immediately (code 2)"),
    ],
)
async def test_setup_session_reports_a_vnc_process_that_exits(
    monkeypatch: pytest.MonkeyPatch,
    returncodes: tuple[int | None, ...],
    message: str,
) -> None:
    class FakeProcess:
        def __init__(self, returncode: int | None) -> None:
            self.returncode = returncode

        def poll(self) -> int | None:
            return self.returncode

    processes = iter(FakeProcess(code) for code in returncodes)
    action_globals = _action_globals(monkeypatch)
    monkeypatch.setitem(action_globals, "has_display", lambda: False)
    monkeypatch.setitem(action_globals, "ensure_xvfb", lambda: None)
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.stop_procs",
        lambda _procs: None,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display._resolve_executables",
        lambda *_names: {"x11vnc": "/usr/bin/x11vnc", "websockify": "/usr/bin/websockify"},
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.subprocess.Popen",
        lambda _args, **_kwargs: next(processes),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._display.time.sleep",
        lambda _seconds: None,
    )

    result = await _handler()({})

    assert result == {"error": message}
