"""Public behavior of the shared system-browser and display utilities."""

from __future__ import annotations

import os
import re
import subprocess  # noqa: S404 - tests exercise subprocess boundary failures.
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from pynchy.plugins.integrations import browser

if TYPE_CHECKING:
    from collections.abc import Callable


class _FakeProcess(subprocess.Popen[bytes]):
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode or 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _TimeoutProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired("browser", timeout or 5)
        return super().wait(timeout)


@pytest.mark.parametrize("configured", [False, True])
def test_project_root_uses_environment_override_or_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: bool,
) -> None:
    if configured:
        monkeypatch.setenv("PYNCHY_PROJECT_ROOT", str(tmp_path))
        assert browser.project_root() == tmp_path
    else:
        monkeypatch.delenv("PYNCHY_PROJECT_ROOT", raising=False)
        assert browser.project_root() == Path.cwd()


def test_chrome_path_accepts_an_existing_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chrome = tmp_path / "chrome"
    chrome.touch()
    monkeypatch.setenv("CHROME_PATH", str(chrome))

    assert browser.chrome_path() == str(chrome)


def test_chrome_path_rejects_a_missing_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHROME_PATH", "/missing/chrome")

    with pytest.raises(RuntimeError, match=re.escape("CHROME_PATH='/missing/chrome'")):
        browser.chrome_path()


def test_chrome_path_reports_platform_install_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(browser, "_detect_chrome", lambda: None)

    with pytest.raises(RuntimeError, match="Install Google Chrome"):
        browser.chrome_path()


def test_profile_dir_creates_a_project_scoped_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PYNCHY_PROJECT_ROOT", str(tmp_path))

    profile = browser.profile_dir("slack")

    assert profile == tmp_path / "data" / "playwright-profiles" / "slack"
    assert profile.is_dir()


def test_chrome_path_uses_platform_fallback_when_no_standard_binary_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(browser.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "is_file", lambda _path: False)
    monkeypatch.setattr(
        browser.shutil,
        "which",
        lambda name: "/opt/chromium" if name == "chromium" else None,
    )

    assert browser.chrome_path() == "/opt/chromium"


def test_chrome_path_prefers_the_first_linux_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: str(path) == "/usr/bin/google-chrome-stable",
    )

    assert browser.chrome_path() == "/usr/bin/google-chrome-stable"


def test_chrome_path_reports_no_browser_after_all_detection_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.setattr(Path, "is_file", lambda _path: False)
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="Chrome/Chromium is not installed"):
        browser.chrome_path()


def test_start_virtual_display_reports_missing_system_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="Headless display requires: Xvfb, x11vnc, websockify"):
        browser.start_virtual_display()


def test_start_virtual_display_reports_only_the_missing_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = {"Xvfb": "/usr/bin/Xvfb", "x11vnc": None, "websockify": "/usr/bin/websockify"}
    monkeypatch.setattr(browser.shutil, "which", paths.__getitem__)

    with pytest.raises(RuntimeError, match="x11vnc"):
        browser.start_virtual_display()


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError, lambda: subprocess.TimeoutExpired("xdpyinfo", 5)],
)
def test_display_probes_treat_process_failures_as_not_live(
    monkeypatch: pytest.MonkeyPatch,
    failure: Callable[[], BaseException],
) -> None:
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(browser.shutil, "which", lambda _name: "/usr/bin/xdpyinfo")
    error = failure()
    monkeypatch.setattr(browser.subprocess, "run", Mock(side_effect=error))

    assert browser.has_display() is False


def test_has_display_returns_false_without_display_or_xdpyinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    assert browser.has_display() is False

    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)
    assert browser.has_display() is False


def test_display_is_live_treats_a_timeout_as_not_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser.shutil, "which", lambda _name: "/usr/bin/xdpyinfo")
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        Mock(side_effect=subprocess.TimeoutExpired("xdpyinfo", 3)),
    )

    assert browser.display_is_live(":99") is False


def test_display_is_live_returns_false_without_xdpyinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)

    assert browser.display_is_live(":99") is False


def test_start_virtual_display_starts_the_complete_vnc_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = [_FakeProcess(), _FakeProcess(), _FakeProcess()]
    popen = Mock(side_effect=processes)
    monkeypatch.setattr(browser.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(browser, "display_is_live", lambda _display: False)
    monkeypatch.setattr(browser.subprocess, "Popen", popen)
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(Path, "is_dir", lambda _path: True)

    started, url = browser.start_virtual_display()

    assert started == processes
    assert os.environ["DISPLAY"] == ":99"
    assert url.endswith(":6080/vnc.html?autoconnect=true")
    assert popen.call_args_list[-1].args[0][1:3] == ["--web", "/usr/share/novnc"]


def test_start_virtual_display_reuses_live_display_and_repairs_vnc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repaired = [_FakeProcess()]
    monkeypatch.setattr(
        browser,
        "resolve_executables",
        lambda *_names: {"Xvfb": "Xvfb", "x11vnc": "x11vnc", "websockify": "websockify"},
    )
    monkeypatch.setattr(browser, "display_is_live", lambda _display: True)
    monkeypatch.setattr(browser, "ensure_vnc_stack_alive", lambda: repaired)

    started, url = browser.start_virtual_display()

    assert started == repaired
    assert url.endswith(":6080/vnc.html?autoconnect=true")


@pytest.mark.parametrize(
    ("failed_index", "expected"),
    [
        (0, "Xvfb exited immediately (code 2)"),
        (1, "x11vnc exited immediately (code 3)"),
        (2, "websockify exited immediately (code 4)"),
    ],
)
def test_start_virtual_display_reports_a_stack_process_that_exits(
    monkeypatch: pytest.MonkeyPatch,
    failed_index: int,
    expected: str,
) -> None:
    processes = [_FakeProcess() for _ in range(failed_index)] + [_FakeProcess(failed_index + 2)]
    monkeypatch.setattr(
        browser,
        "resolve_executables",
        lambda *_names: {"Xvfb": "Xvfb", "x11vnc": "x11vnc", "websockify": "websockify"},
    )
    monkeypatch.setattr(browser, "display_is_live", lambda _display: False)
    monkeypatch.setattr(browser.subprocess, "Popen", Mock(side_effect=processes))
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)

    with pytest.raises(RuntimeError, match=re.escape(expected)):
        browser.start_virtual_display()


def test_ensure_vnc_stack_alive_repairs_both_dead_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = [_FakeProcess(), _FakeProcess()]
    paths = {"pgrep": None, "x11vnc": "/usr/bin/x11vnc", "websockify": "/usr/bin/websockify"}
    monkeypatch.setattr(browser.shutil, "which", paths.__getitem__)
    monkeypatch.setattr(browser.subprocess, "Popen", Mock(side_effect=processes))
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)

    repaired = browser.ensure_vnc_stack_alive()

    assert repaired == processes
    assert processes[0].returncode is None
    assert processes[1].returncode is None


def test_ensure_vnc_stack_alive_includes_web_directory_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    paths = {"pgrep": None, "x11vnc": "x11vnc", "websockify": "websockify"}
    popen = Mock(return_value=process)
    monkeypatch.setattr(browser.shutil, "which", paths.__getitem__)
    monkeypatch.setattr(browser.subprocess, "Popen", popen)
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(Path, "is_dir", lambda _path: True)

    browser.ensure_vnc_stack_alive()

    assert popen.call_args_list[-1].args[0][1:3] == ["--web", "/usr/share/novnc"]


def test_ensure_vnc_stack_alive_does_nothing_when_both_processes_are_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser, "_is_process_running", lambda _name: True)

    assert browser.ensure_vnc_stack_alive() == []


@pytest.mark.parametrize(
    ("already_running", "missing"), [("x11vnc", "websockify"), ("websockify", "x11vnc")]
)
def test_vnc_repair_starts_only_the_missing_process(monkeypatch, already_running, missing):
    process = _FakeProcess()
    popen = Mock(return_value=process)
    monkeypatch.setattr(browser, "_is_process_running", lambda name: name == already_running)
    monkeypatch.setattr(browser.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(browser.subprocess, "Popen", popen)
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)

    assert browser.ensure_vnc_stack_alive() == [process]
    popen.assert_called_once()
    assert popen.call_args.args[0][0] == f"/usr/bin/{missing}"


@pytest.mark.parametrize("failure", ["exit", "spawn"])
def test_vnc_repair_cleans_up_partial_startup(monkeypatch, failure):
    running = _FakeProcess()
    failed = _FakeProcess(7) if failure == "exit" else OSError("cannot launch websockify")
    monkeypatch.setattr(browser, "_is_process_running", lambda _name: False)
    monkeypatch.setattr(browser.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(browser.subprocess, "Popen", Mock(side_effect=[running, failed]))
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)

    error = RuntimeError if failure == "exit" else OSError
    with pytest.raises(error, match="websockify"):
        browser.ensure_vnc_stack_alive()

    assert running.terminated is True


def test_ensure_vnc_stack_alive_reports_a_missing_repair_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser, "_is_process_running", lambda _name: False)
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="x11vnc"):
        browser.ensure_vnc_stack_alive()


def test_ensure_vnc_stack_alive_uses_pgrep_when_tools_are_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = {"pgrep": "/usr/bin/pgrep", "x11vnc": "x11vnc", "websockify": "websockify"}
    monkeypatch.setattr(browser.shutil, "which", paths.__getitem__)
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0)),
    )

    assert browser.ensure_vnc_stack_alive() == []


def test_stop_procs_kills_a_process_that_ignores_termination() -> None:
    process = _TimeoutProcess()

    browser.stop_procs([process])

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2


def test_cleanup_lock_files_removes_stale_chromium_locks(tmp_path: Path) -> None:
    (tmp_path / "SingletonLock").symlink_to("old-pod-42")
    for name in ("SingletonSocket", "SingletonCookie"):
        (tmp_path / name).touch()

    browser.cleanup_lock_files(tmp_path)

    assert list(tmp_path.iterdir()) == []
    browser.cleanup_lock_files(tmp_path)


def test_check_browser_plugin_deps_warns_and_returns_when_chrome_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warning = Mock()
    monkeypatch.setattr(browser, "chrome_path", Mock(side_effect=RuntimeError("not installed")))
    monkeypatch.setattr(browser.logger, "warning", warning)

    browser.check_browser_plugin_deps("x")

    warning.assert_called_once_with(
        "system dep check failed",
        service_name="x",
        error="not installed",
    )


def test_check_browser_plugin_deps_warns_about_missing_headless_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser, "chrome_path", lambda: "/usr/bin/google-chrome")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)
    warning = Mock()
    monkeypatch.setattr(browser.logger, "warning", warning)

    browser.check_browser_plugin_deps("slack")

    warning.assert_called_once_with(
        "headless server needs VNC deps",
        service_name="slack",
        missing=["Xvfb", "x11vnc", "websockify"],
    )


def test_check_browser_plugin_deps_is_quiet_when_dependencies_are_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser, "chrome_path", lambda: "/usr/bin/google-chrome")
    warning = Mock()
    monkeypatch.setattr(browser.logger, "warning", warning)
    monkeypatch.setenv("DISPLAY", ":99")

    browser.check_browser_plugin_deps("slack")

    monkeypatch.delenv("DISPLAY")
    monkeypatch.setattr(browser.shutil, "which", lambda _name: "/usr/bin/tool")
    browser.check_browser_plugin_deps("slack")

    warning.assert_not_called()
