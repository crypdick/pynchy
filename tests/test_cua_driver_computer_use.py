"""Compatibility tests for the Cua Driver computer-use provider plugin."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins.api import ComputerUseRouterConfig
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


def _handler(data_dir: Path | None = None) -> HostActionHandler:
    backend = CuaDriverBackend(CuaDriverConfig(binary="cua-driver", timeout_seconds=5))
    config = ComputerUseRouterConfig(providers=("cua-driver",))
    registration = ComputerUsePlugin(config, data_dir=data_dir).pynchy_service_handler((backend,))
    return registration.actions[0].handler


def test_plugin_uses_lifecycle_configuration() -> None:
    plugin = CuaDriverComputerUsePlugin()
    config = CuaDriverConfig(binary="configured-cua", timeout_seconds=5)

    plugin.configure(config)

    assert plugin.pynchy_computer_use_backend().config is config


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
async def test_existing_actions_remain_available_through_cua_fallback(
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
