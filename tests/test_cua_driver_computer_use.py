"""Compatibility tests for the Cua Driver computer-use provider plugin."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins.api import ComputerUseConfig
from pynchy.plugins.integrations.computer_use import ComputerUsePlugin
from pynchy.plugins.integrations.cua_driver import (
    CuaDriverBackend,
    CuaDriverComputerUsePlugin,
    CuaDriverConfig,
)

if TYPE_CHECKING:
    from pynchy.plugins.api import HostActionHandler


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
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


def _handler(data_dir: Path | None = None, *, timeout_seconds: float = 5) -> HostActionHandler:
    backend = CuaDriverBackend(
        CuaDriverConfig(binary="cua-driver", timeout_seconds=timeout_seconds)
    )
    config = ComputerUseConfig(provider="cua-driver")
    registration = ComputerUsePlugin(config, data_dir=data_dir).pynchy_service_handler((backend,))
    return registration.actions[0].handler


@pytest.fixture(autouse=True)
def _cua_driver_runs_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.cua_driver.platform.system", lambda: "Darwin")


def test_plugin_uses_lifecycle_configuration() -> None:
    plugin = CuaDriverComputerUsePlugin()
    config = CuaDriverConfig(binary="configured-cua", timeout_seconds=5)

    plugin.configure(config)

    assert plugin.pynchy_computer_use_backend().config is config


def test_cua_availability_reports_platform_and_binary_requirements() -> None:
    backend = CuaDriverBackend(CuaDriverConfig(binary="configured-cua"))

    with patch("pynchy.plugins.integrations.cua_driver.platform.system", return_value="Linux"):
        availability = backend.availability()
        assert availability.available is False
        assert availability.reason == "Cua Driver requires macOS"

    with (
        patch("pynchy.plugins.integrations.cua_driver.platform.system", return_value="Darwin"),
        patch("pynchy.plugins.integrations.cua_driver.shutil.which", return_value=None),
    ):
        availability = backend.availability()
        assert availability.available is False
        assert availability.reason == "Cua Driver is not installed at 'configured-cua'"

    with (
        patch("pynchy.plugins.integrations.cua_driver.platform.system", return_value="Darwin"),
        patch("pynchy.plugins.integrations.cua_driver.shutil.which", return_value="/bin/cua"),
    ):
        assert backend.availability().available is True


@pytest.mark.asyncio
async def test_cua_reports_missing_binary_at_execution_time() -> None:
    with patch("pynchy.plugins.integrations.cua_driver.shutil.which", return_value=None):
        result = await _handler()({"source_group": "admin", "action": "list_apps"})

    assert result == {"error": "Cua Driver is not installed at 'cua-driver'"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "arguments", "expected_action"),
    [
        ("capture", {"pid": 844}, "get_window_state"),
        ("list_apps", {}, "list_apps"),
        ("list_windows", {}, "list_windows"),
        ("launch_app", {"bundle_id": "com.apple.TextEdit"}, "launch_app"),
        ("click", {"element": 14}, "click"),
        ("double_click", {"coordinate": [12, 34]}, "double_click"),
        ("right_click", {"coordinate": [12, 34]}, "right_click"),
        ("type", {"pid": 844, "text": "hello"}, "type_text"),
        ("key", {"pid": 844, "keys": "cmd+s"}, "hotkey"),
        ("scroll", {"pid": 844, "delta_y": -240}, "scroll"),
        ("check_permissions", {}, "check_permissions"),
    ],
)
async def test_existing_actions_remain_available_through_cua_provider(
    action: str,
    arguments: dict[str, object],
    expected_action: str,
    tmp_path: Path,
) -> None:
    captured_calls: list[tuple[str, ...]] = []

    def fake_exec(*args: str, **_kwargs: object) -> _FakeProcess:
        captured_calls.append(args)
        if "--screenshot-out-file" in args:
            Path(args[-1]).write_bytes(b"png bytes")
        return _FakeProcess(stdout=b"action completed")

    with (
        patch("pynchy.plugins.integrations.cua_driver.platform.system", return_value="Darwin"),
        patch(
            "pynchy.plugins.integrations.cua_driver.shutil.which",
            return_value="/bin/cua-driver",
        ),
        patch(
            "pynchy.plugins.integrations.cua_driver.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
    ):
        result = await _handler(tmp_path)({"source_group": "admin", "action": action, **arguments})

    assert captured_calls[0][0:3] == ("/bin/cua-driver", "call", expected_action)
    assert result["result"]["backend"] == "cua-driver"
    assert result["result"]["output"] == "action completed"


@pytest.mark.asyncio
async def test_cua_key_preserves_shortcut_payload() -> None:
    captured_args: tuple[str, ...] | None = None

    def fake_exec(*args: str, **_kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        return _FakeProcess(stdout=b"sent")

    with (
        patch("pynchy.plugins.integrations.cua_driver.platform.system", return_value="Darwin"),
        patch(
            "pynchy.plugins.integrations.cua_driver.shutil.which",
            return_value="/bin/cua-driver",
        ),
        patch(
            "pynchy.plugins.integrations.cua_driver.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
    ):
        result = await _handler()(
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
async def test_cua_capture_includes_optional_window_filters(tmp_path: Path) -> None:
    captured_args: tuple[str, ...] | None = None

    def fake_exec(*args: str, **_kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        if "--screenshot-out-file" in args:
            Path(args[-1]).write_bytes(b"png bytes")
        return _FakeProcess()

    with (
        patch(
            "pynchy.plugins.integrations.cua_driver.shutil.which", return_value="/bin/cua-driver"
        ),
        patch(
            "pynchy.plugins.integrations.cua_driver.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
    ):
        result = await _handler(tmp_path)(
            {
                "source_group": "admin",
                "action": "capture",
                "pid": 844,
                "window_id": 2,
                "query": "Editor",
            }
        )

    assert captured_args is not None
    assert json.loads(captured_args[3]) == {
        "pid": 844,
        "window_id": 2,
        "query": "Editor",
    }
    assert result["result"]["cua_action"] == "get_window_state"


@pytest.mark.asyncio
async def test_cua_capture_requires_pid(tmp_path: Path) -> None:
    with patch(
        "pynchy.plugins.integrations.cua_driver.shutil.which", return_value="/bin/cua-driver"
    ):
        result = await _handler(tmp_path)({"source_group": "admin", "action": "capture"})

    assert result == {"error": "Cua Driver capture requires pid"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "arguments", "message"),
    [
        (
            "click",
            {"element": "submit"},
            "stable element references require another computer-use provider",
        ),
        (
            "click",
            {"query": "submit"},
            "Cua Driver click actions require numeric element or coordinate",
        ),
    ],
)
async def test_cua_click_rejects_unsupported_targets(
    action: str, arguments: dict[str, object], message: str
) -> None:
    with patch(
        "pynchy.plugins.integrations.cua_driver.shutil.which", return_value="/bin/cua-driver"
    ):
        result = await _handler()({"source_group": "admin", "action": action, **arguments})

    assert result == {"error": message}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "expected_key", "expected_value"),
    [
        ("down", "delta_y", -360),
        ("up", "delta_y", 360),
        ("left", "delta_x", -360),
        ("right", "delta_x", 360),
    ],
)
async def test_cua_scroll_direction_becomes_driver_delta(
    direction: str, expected_key: str, expected_value: int
) -> None:
    captured_args: tuple[str, ...] | None = None

    def fake_exec(*args: str, **_kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        return _FakeProcess()

    with (
        patch(
            "pynchy.plugins.integrations.cua_driver.shutil.which", return_value="/bin/cua-driver"
        ),
        patch(
            "pynchy.plugins.integrations.cua_driver.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
    ):
        await _handler()({"source_group": "admin", "action": "scroll", "direction": direction})

    assert captured_args is not None
    assert json.loads(captured_args[3])[expected_key] == expected_value


@pytest.mark.asyncio
async def test_cua_key_accepts_tuple_shortcuts() -> None:
    captured_args: tuple[str, ...] | None = None

    def fake_exec(*args: str, **_kwargs: object) -> _FakeProcess:
        nonlocal captured_args
        captured_args = args
        return _FakeProcess()

    with (
        patch(
            "pynchy.plugins.integrations.cua_driver.shutil.which", return_value="/bin/cua-driver"
        ),
        patch(
            "pynchy.plugins.integrations.cua_driver.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
    ):
        await _handler()({"source_group": "admin", "action": "key", "keys": ("CMD", "Shift")})

    assert captured_args is not None
    assert json.loads(captured_args[3])["keys"] == ["cmd", "shift"]


@pytest.mark.asyncio
async def test_cua_rejects_peekaboo_only_action() -> None:
    with (
        patch("pynchy.plugins.integrations.cua_driver.platform.system", return_value="Darwin"),
        patch(
            "pynchy.plugins.integrations.cua_driver.shutil.which",
            return_value="/bin/cua-driver",
        ),
    ):
        result = await _handler()(
            {
                "source_group": "admin",
                "action": "set_value",
                "element": "T1",
                "value": "hello",
            }
        )

    assert result == {
        "error": (
            "set_value requires another computer-use provider; Cua Driver does not support it"
        )
    }


@pytest.mark.asyncio
async def test_cua_command_failure_is_returned() -> None:
    with (
        patch("pynchy.plugins.integrations.cua_driver.platform.system", return_value="Darwin"),
        patch(
            "pynchy.plugins.integrations.cua_driver.shutil.which",
            return_value="/bin/cua-driver",
        ),
        patch(
            "pynchy.plugins.integrations.cua_driver.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess(returncode=1, stderr=b"permission denied")),
        ),
    ):
        result = await _handler()({"source_group": "admin", "action": "list_apps"})

    assert result == {"error": "cua-driver list_apps failed: permission denied"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (b"driver reported failure", "driver reported failure"),
        (b"", "exit code 1"),
    ],
)
async def test_cua_command_failure_uses_output_or_exit_code(stdout: bytes, expected: str) -> None:
    with (
        patch(
            "pynchy.plugins.integrations.cua_driver.shutil.which", return_value="/bin/cua-driver"
        ),
        patch(
            "pynchy.plugins.integrations.cua_driver.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess(returncode=1, stdout=stdout)),
        ),
    ):
        result = await _handler()({"source_group": "admin", "action": "list_apps"})

    assert result == {"error": f"cua-driver list_apps failed: {expected}"}


@pytest.mark.asyncio
async def test_cua_command_timeout_kills_process() -> None:
    class _TimeoutProcess(_FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.communicate_calls = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                await asyncio.sleep(1)
            return b"", b""

    process = _TimeoutProcess()
    with (
        patch(
            "pynchy.plugins.integrations.cua_driver.shutil.which", return_value="/bin/cua-driver"
        ),
        patch(
            "pynchy.plugins.integrations.cua_driver.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    ):
        result = await _handler(timeout_seconds=0.01)(
            {"source_group": "admin", "action": "list_apps"}
        )

    assert result == {"error": "cua-driver list_apps timed out after 0.01s"}
    assert process.killed is True
    assert process.communicate_calls == 2
