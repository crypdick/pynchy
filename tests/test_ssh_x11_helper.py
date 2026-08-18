"""Behavioral tests for the packaged SSH X11 helper."""

from __future__ import annotations

import io
import json
import subprocess  # noqa: S404 - tests construct inert CompletedProcess results.
from typing import Any
from unittest.mock import MagicMock

import pytest

from pynchy.plugins.integrations import ssh_x11_helper


def _run_recorder(calls: list[list[str]]):
    def run(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        del env
        calls.append(argv)
        if argv == ["wmctrl", "-lxp"]:
            stdout = b"malformed\n0x01 0 123 brave.Brave host myEDD - Brave\n"
        elif argv[0] == "import":
            stdout = b"png"
        elif argv == ["xdotool", "getactivewindow"]:
            stdout = b"1\n"
        else:
            stdout = b""
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    return run


def test_permission_handshake_reports_version_and_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        ssh_x11_helper, "_x11_environment", lambda: {"PATH": "/bin", "DISPLAY": ":0"}
    )
    monkeypatch.setattr(ssh_x11_helper, "_require_binaries", lambda *args, **kwargs: None)
    monkeypatch.setattr(ssh_x11_helper, "_run", _run_recorder(calls))

    result = ssh_x11_helper.command({"action": "check_permissions"})

    assert result["protocol_version"] == ssh_x11_helper.PROTOCOL_VERSION
    assert set(result["supported_actions"]) == ssh_x11_helper.SUPPORTED_ACTIONS
    assert result["ready"] is True
    assert calls == [["xdotool", "getactivewindow"]]


def test_launch_app_opens_web_urls_with_requested_app(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    popen = MagicMock()
    monkeypatch.setattr(
        ssh_x11_helper, "_x11_environment", lambda: {"PATH": "/bin", "DISPLAY": ":0"}
    )
    monkeypatch.setattr(ssh_x11_helper, "_run", _run_recorder(calls))
    monkeypatch.setattr(ssh_x11_helper, "_require_binaries", lambda *args, **kwargs: None)
    monkeypatch.setattr(ssh_x11_helper.shutil, "which", lambda *args, **kwargs: "/snap/bin/brave")
    monkeypatch.setattr(ssh_x11_helper.subprocess, "Popen", popen)

    result = ssh_x11_helper.command(
        {
            "action": "launch_app",
            "app": "Brave",
            "urls": ["https://myedd.edd.ca.gov/"],
        }
    )

    assert popen.call_args.args[0] == ["/snap/bin/brave", "https://myedd.edd.ca.gov/"]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["env"] == {"PATH": "/bin", "DISPLAY": ":0"}
    assert calls == []
    assert result == {"launched": True, "urls": ["https://myedd.edd.ca.gov/"]}


@pytest.mark.parametrize(
    ("payload", "expected_command"),
    [
        (
            {"action": "capture", "app": "Brave"},
            ["import", "-silent", "-window", "root", "png:-"],
        ),
        (
            {"action": "click", "coordinate": [10, 20]},
            ["xdotool", "mousemove", "--sync", "10", "20", "click", "--repeat", "1", "1"],
        ),
        (
            {"action": "double_click", "coordinate": [10, 20]},
            ["xdotool", "mousemove", "--sync", "10", "20", "click", "--repeat", "2", "1"],
        ),
        (
            {"action": "right_click", "coordinate": [10, 20]},
            ["xdotool", "mousemove", "--sync", "10", "20", "click", "--repeat", "1", "3"],
        ),
        (
            {"action": "type", "text": "hello"},
            ["xdotool", "type", "--clearmodifiers", "--delay", "1", "--", "hello"],
        ),
        (
            {"action": "type", "text": "hello", "clear": True},
            ["xdotool", "key", "--clearmodifiers", "ctrl+a"],
        ),
        (
            {"action": "key", "keys": "cmd+s"},
            ["xdotool", "key", "--clearmodifiers", "super+s"],
        ),
        (
            {"action": "scroll", "direction": "down", "amount": 2},
            ["xdotool", "click", "--repeat", "2", "5"],
        ),
        (
            {"action": "scroll", "delta_y": -120},
            ["xdotool", "click", "--repeat", "1", "5"],
        ),
    ],
)
def test_mutating_and_capture_actions_use_closed_argv(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    expected_command: list[str],
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        ssh_x11_helper, "_x11_environment", lambda: {"PATH": "/bin", "DISPLAY": ":0"}
    )
    monkeypatch.setattr(ssh_x11_helper, "_run", _run_recorder(calls))
    monkeypatch.setattr(ssh_x11_helper.time, "sleep", lambda _seconds: None)

    ssh_x11_helper.command(payload)

    assert expected_command in calls


def test_list_actions_parse_real_wmctrl_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        ssh_x11_helper, "_x11_environment", lambda: {"PATH": "/bin", "DISPLAY": ":0"}
    )
    monkeypatch.setattr(ssh_x11_helper, "_run", _run_recorder(calls))

    apps = ssh_x11_helper.command({"action": "list_apps"})
    windows = ssh_x11_helper.command({"action": "list_windows", "app": "Brave"})

    assert apps == {"apps": ["brave.Brave"]}
    assert windows["windows"][0]["title"] == "myEDD - Brave"


def test_window_index_rejects_untyped_remote_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ssh_x11_helper, "_x11_environment", lambda: {"PATH": "/bin", "DISPLAY": ":0"}
    )
    monkeypatch.setattr(ssh_x11_helper, "_run", _run_recorder([]))

    with pytest.raises(ValueError, match="window_index"):
        ssh_x11_helper.command({"action": "capture", "app": "Brave", "window_index": "0"})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"action": "unknown"}, "does not implement"),
        ({"action": "click"}, "coordinate"),
        ({"action": "capture", "app": "Firefox"}, "no matching"),
        ({"action": "capture", "app": "Brave", "window_index": 1}, "exceeds"),
        ({"action": "launch_app", "app": "Brave", "urls": []}, "at least one URL"),
        ({"action": "launch_app", "app": "Brave", "urls": ["file:///etc/passwd"]}, "HTTP"),
        (
            {"action": "launch_app", "app": "Missing", "urls": ["https://example.com"]},
            "not installed",
        ),
    ],
)
def test_helper_rejects_invalid_requests(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    message: str,
) -> None:
    monkeypatch.setattr(
        ssh_x11_helper, "_x11_environment", lambda: {"PATH": "/bin", "DISPLAY": ":0"}
    )
    monkeypatch.setattr(ssh_x11_helper, "_run", _run_recorder([]))

    with pytest.raises(ValueError, match=message):
        ssh_x11_helper.command(payload)


@pytest.mark.parametrize("local_xdotool", [False, True])
def test_permission_handshake_builds_x11_environment_and_runs_closed_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    local_xdotool: bool,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(ssh_x11_helper.Path, "is_dir", lambda _path: local_xdotool)
    monkeypatch.setattr(ssh_x11_helper.shutil, "which", lambda _name, *, path: "/bin/tool")
    completed = subprocess.CompletedProcess(["xdotool", "getactivewindow"], 0, b"1\n", b"")
    subprocess_run = MagicMock(return_value=completed)
    monkeypatch.setattr(ssh_x11_helper.subprocess, "run", subprocess_run)

    result = ssh_x11_helper.command({"action": "check_permissions"})

    env = subprocess_run.call_args.kwargs["env"]
    assert result["ready"] is True
    assert subprocess_run.call_args.args[0] == ["xdotool", "getactivewindow"]
    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"] == str(home / ".Xauthority")
    if local_xdotool:
        assert env["PATH"].startswith(f"{home}/.local/opt/xdotool/usr/bin:")
        assert env["LD_LIBRARY_PATH"].startswith(
            f"{home}/.local/opt/xdotool/usr/lib/x86_64-linux-gnu:"
        )
    else:
        assert env["PATH"] == "/usr/bin"


def test_helper_reports_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_x11_helper.shutil, "which", lambda _name, *, path: None)

    with pytest.raises(RuntimeError, match="missing desktop binaries"):
        ssh_x11_helper.command({"action": "check_permissions"})


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [({"action": "list_apps"}, 0), ({"action": "unknown"}, 1), (["not", "an", "object"], 1)],
)
def test_helper_main_serializes_result(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    expected_status: int,
) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr(ssh_x11_helper.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(ssh_x11_helper.sys, "stdout", stdout)
    monkeypatch.setattr(
        ssh_x11_helper, "_x11_environment", lambda: {"PATH": "/bin", "DISPLAY": ":0"}
    )
    monkeypatch.setattr(ssh_x11_helper, "_run", _run_recorder([]))

    status = ssh_x11_helper.main()

    assert status == expected_status
    assert isinstance(json.loads(stdout.getvalue()), dict)
