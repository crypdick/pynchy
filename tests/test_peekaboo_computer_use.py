"""Hermetic contract tests for the Peekaboo computer-use provider plugin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins.api import ComputerUseConfig
from pynchy.plugins.integrations.computer_use import ComputerUsePlugin
from pynchy.plugins.integrations.peekaboo import (
    PeekabooBackend,
    PeekabooComputerUsePlugin,
    PeekabooConfig,
)

if TYPE_CHECKING:
    from pynchy.plugins.api import HostActionHandler


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes | None = None,
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout or json.dumps({"success": True, "data": {"ok": True}}).encode()
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


def _handler(data_dir: Path | None = None) -> HostActionHandler:
    backend = PeekabooBackend(PeekabooConfig(binary="peekaboo", timeout_seconds=5))
    config = ComputerUseConfig(provider="peekaboo")
    registration = ComputerUsePlugin(config, data_dir=data_dir).pynchy_service_handler((backend,))
    return registration.actions[0].handler


def test_plugin_uses_lifecycle_configuration() -> None:
    plugin = PeekabooComputerUsePlugin()
    config = PeekabooConfig(binary="configured-peekaboo", timeout_seconds=5)

    plugin.configure(config)

    assert plugin.pynchy_computer_use_backend().config is config


_CASES = [
    pytest.param(
        "capture",
        {"pid": 844, "window_id": 10725, "label": "Calculator"},
        (
            "/bin/peekaboo",
            "see",
            "--pid",
            "844",
            "--window-id",
            "10725",
            "--path",
            "$ARTIFACT",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.capture"),
        id="capture",
    ),
    pytest.param(
        "list_apps",
        {},
        ("/bin/peekaboo", "list", "apps", "--json"),
        marks=pytest.mark.action("desktop.computer.app.list"),
        id="list-apps",
    ),
    pytest.param(
        "list_windows",
        {"app": "Finder"},
        ("/bin/peekaboo", "list", "windows", "--app", "Finder", "--json"),
        marks=pytest.mark.action("desktop.computer.window.list"),
        id="list-windows",
    ),
    pytest.param(
        "launch_app",
        {
            "bundle_id": "com.apple.TextEdit",
            "urls": ["https://example.com"],
            "wait_until_ready": True,
        },
        (
            "/bin/peekaboo",
            "app",
            "launch",
            "--bundle-id",
            "com.apple.TextEdit",
            "--open",
            "https://example.com",
            "--wait-until-ready",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.app.launch"),
        id="launch-app",
    ),
    pytest.param(
        "click",
        {"pid": 844, "element": "B1", "snapshot_id": "snap-1"},
        ("/bin/peekaboo", "click", "--on", "B1", "--pid", "844", "--snapshot", "snap-1", "--json"),
        marks=pytest.mark.action("desktop.computer.click"),
        id="click",
    ),
    pytest.param(
        "double_click",
        {"pid": 844, "coordinate": [12, 34]},
        (
            "/bin/peekaboo",
            "click",
            "--coords",
            "12,34",
            "--pid",
            "844",
            "--double",
            "--foreground",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.double.click"),
        id="double-click",
    ),
    pytest.param(
        "right_click",
        {"app": "Safari", "query": "Allow"},
        ("/bin/peekaboo", "click", "Allow", "--app", "Safari", "--right", "--json"),
        marks=pytest.mark.action("desktop.computer.right.click"),
        id="right-click",
    ),
    pytest.param(
        "type",
        {"app": "TextEdit", "snapshot_id": "snap-1", "text": "hello", "clear": True},
        (
            "/bin/peekaboo",
            "type",
            "--text",
            "hello",
            "--app",
            "TextEdit",
            "--snapshot",
            "snap-1",
            "--clear",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.text.type"),
        id="type",
    ),
    pytest.param(
        "key",
        {"app": "Finder", "keys": ["cmd", "shift", "p"]},
        ("/bin/peekaboo", "hotkey", "--keys", "cmd,shift,p", "--app", "Finder", "--json"),
        marks=pytest.mark.action("desktop.computer.key.send"),
        id="key",
    ),
    pytest.param(
        "scroll",
        {
            "app": "Safari",
            "element": "B2",
            "snapshot_id": "snap-1",
            "direction": "down",
            "amount": 5,
            "smooth": True,
        },
        (
            "/bin/peekaboo",
            "scroll",
            "--direction",
            "down",
            "--amount",
            "5",
            "--on",
            "B2",
            "--app",
            "Safari",
            "--snapshot",
            "snap-1",
            "--smooth",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.scroll"),
        id="scroll",
    ),
    pytest.param(
        "set_value",
        {"element": "T1", "value": "hello", "snapshot_id": "snap-1"},
        (
            "/bin/peekaboo",
            "set-value",
            "--value",
            "hello",
            "--on",
            "T1",
            "--snapshot",
            "snap-1",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.element.value.set"),
        id="set-value",
    ),
    pytest.param(
        "perform_action",
        {"element": "B1", "accessibility_action": "AXPress", "snapshot_id": "snap-1"},
        (
            "/bin/peekaboo",
            "perform-action",
            "--on",
            "B1",
            "--action",
            "AXPress",
            "--snapshot",
            "snap-1",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.element.action.perform"),
        id="perform-action",
    ),
    pytest.param(
        "menu_list",
        {"app": "Finder", "include_disabled": True},
        ("/bin/peekaboo", "menu", "list", "--app", "Finder", "--include-disabled", "--json"),
        marks=pytest.mark.action("desktop.computer.menu.list"),
        id="menu-list",
    ),
    pytest.param(
        "menu_click",
        {"app": "Safari", "menu_path": "File > New Window"},
        (
            "/bin/peekaboo",
            "menu",
            "click",
            "--app",
            "Safari",
            "--path",
            "File > New Window",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.menu.click"),
        id="menu-click",
    ),
    pytest.param(
        "dialog_list",
        {"app": "TextEdit"},
        ("/bin/peekaboo", "dialog", "list", "--app", "TextEdit", "--json"),
        marks=pytest.mark.action("desktop.computer.dialog.list"),
        id="dialog-list",
    ),
    pytest.param(
        "dialog_click",
        {"app": "TextEdit", "button": "Don't Save"},
        (
            "/bin/peekaboo",
            "dialog",
            "click",
            "--app",
            "TextEdit",
            "--button",
            "Don't Save",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.dialog.click"),
        id="dialog-click",
    ),
    pytest.param(
        "dialog_input",
        {"app": "Safari", "text": "secret", "field": "Password", "clear": True},
        (
            "/bin/peekaboo",
            "dialog",
            "input",
            "--app",
            "Safari",
            "--text",
            "secret",
            "--field",
            "Password",
            "--clear",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.dialog.input"),
        id="dialog-input",
    ),
    pytest.param(
        "dialog_file",
        {
            "app": "TextEdit",
            "path": "/Users/operator/Documents",
            "name": "note.txt",
            "select": "Save",
            "ensure_expanded": True,
        },
        (
            "/bin/peekaboo",
            "dialog",
            "file",
            "--app",
            "TextEdit",
            "--path",
            "/Users/operator/Documents",
            "--name",
            "note.txt",
            "--select",
            "Save",
            "--ensure-expanded",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.dialog.file"),
        id="dialog-file",
    ),
    pytest.param(
        "dialog_dismiss",
        {"app": "TextEdit", "force": True},
        ("/bin/peekaboo", "dialog", "dismiss", "--app", "TextEdit", "--force", "--json"),
        marks=pytest.mark.action("desktop.computer.dialog.dismiss"),
        id="dialog-dismiss",
    ),
    pytest.param(
        "clipboard_get",
        {"prefer": "public.utf8-plain-text"},
        ("/bin/peekaboo", "clipboard", "get", "--prefer", "public.utf8-plain-text", "--json"),
        marks=pytest.mark.action("desktop.computer.clipboard.get"),
        id="clipboard-get",
    ),
    pytest.param(
        "clipboard_set",
        {"text": "hello", "verify": True},
        ("/bin/peekaboo", "clipboard", "set", "--text", "hello", "--verify", "--json"),
        marks=pytest.mark.action("desktop.computer.clipboard.set"),
        id="clipboard-set",
    ),
    pytest.param(
        "clipboard_clear",
        {},
        ("/bin/peekaboo", "clipboard", "clear", "--json"),
        marks=pytest.mark.action("desktop.computer.clipboard.clear"),
        id="clipboard-clear",
    ),
    pytest.param(
        "clipboard_save",
        {"slot": "original"},
        ("/bin/peekaboo", "clipboard", "save", "--slot", "original", "--json"),
        marks=pytest.mark.action("desktop.computer.clipboard.save"),
        id="clipboard-save",
    ),
    pytest.param(
        "clipboard_restore",
        {"slot": "original"},
        ("/bin/peekaboo", "clipboard", "restore", "--slot", "original", "--json"),
        marks=pytest.mark.action("desktop.computer.clipboard.restore"),
        id="clipboard-restore",
    ),
    pytest.param(
        "space_list",
        {"detailed": True},
        ("/bin/peekaboo", "space", "list", "--detailed", "--json"),
        marks=pytest.mark.action("desktop.computer.space.list"),
        id="space-list",
    ),
    pytest.param(
        "space_switch",
        {"space": 2},
        ("/bin/peekaboo", "space", "switch", "--to", "2", "--json"),
        marks=pytest.mark.action("desktop.computer.space.switch"),
        id="space-switch",
    ),
    pytest.param(
        "space_move_window",
        {"app": "Safari", "window_title": "Docs", "space": 3, "follow": True},
        (
            "/bin/peekaboo",
            "space",
            "move-window",
            "--app",
            "Safari",
            "--window-title",
            "Docs",
            "--to",
            "3",
            "--follow",
            "--json",
        ),
        marks=pytest.mark.action("desktop.computer.space.window.move"),
        id="space-move-window",
    ),
    pytest.param(
        "check_permissions",
        {},
        ("/bin/peekaboo", "permissions", "status", "--json"),
        marks=pytest.mark.action("desktop.computer.permissions.check"),
        id="check-permissions",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "arguments", "expected_command"), _CASES)
async def test_each_semantic_action_maps_to_closed_peekaboo_argv(
    action: str,
    arguments: dict[str, object],
    expected_command: tuple[str, ...],
    tmp_path: Path,
) -> None:
    captured_calls: list[tuple[str, ...]] = []

    def fake_exec(*args: str, **_kwargs: object) -> _FakeProcess:
        captured_calls.append(args)
        if action == "capture" and "--path" in args:
            Path(args[args.index("--path") + 1]).write_bytes(b"png bytes")
        return _FakeProcess()

    with (
        patch("pynchy.plugins.integrations.peekaboo.platform.system", return_value="Darwin"),
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value="/bin/peekaboo"),
        patch(
            "pynchy.plugins.integrations.peekaboo.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_exec),
        ),
        patch(
            "pynchy.plugins.integrations.computer_use._plugin._timestamp",
            return_value="20260718T120000Z",
        ),
    ):
        result = await _handler(tmp_path)({"source_group": "admin", "action": action, **arguments})

    artifact = tmp_path / "ipc" / "admin" / "computer-use" / "20260718T120000Z-calculator.png"
    expected = tuple(str(artifact) if item == "$ARTIFACT" else item for item in expected_command)
    assert captured_calls == [expected]
    assert result["result"]["backend"] == "peekaboo"
    assert result["result"]["action"] == action
    assert result["result"]["output"] == {"ok": True}
    if action == "capture":
        assert result["result"]["screenshot"]["container_path"].endswith(
            "20260718T120000Z-calculator.png"
        )


@pytest.mark.asyncio
async def test_peekaboo_structured_failure_is_returned_without_fallback() -> None:
    failure = {"success": False, "error": {"code": "PERMISSION_DENIED", "message": "denied"}}
    with (
        patch("pynchy.plugins.integrations.peekaboo.platform.system", return_value="Darwin"),
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value="/bin/peekaboo"),
        patch(
            "pynchy.plugins.integrations.peekaboo.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess(stdout=json.dumps(failure).encode())),
        ),
    ):
        result = await _handler()({"source_group": "admin", "action": "list_apps"})

    assert result == {"error": "Peekaboo failed: denied"}


@pytest.mark.asyncio
async def test_peekaboo_list_windows_requires_a_target() -> None:
    with (
        patch("pynchy.plugins.integrations.peekaboo.platform.system", return_value="Darwin"),
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value="/bin/peekaboo"),
    ):
        result = await _handler()({"source_group": "admin", "action": "list_windows"})

    assert result == {"error": "Peekaboo list_windows requires app or pid"}


@pytest.mark.asyncio
async def test_peekaboo_click_accepts_a_coordinate_target() -> None:
    process = AsyncMock(return_value=_FakeProcess())
    with (
        patch("pynchy.plugins.integrations.peekaboo.platform.system", return_value="Darwin"),
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value="/bin/peekaboo"),
        patch(
            "pynchy.plugins.integrations.peekaboo.asyncio.create_subprocess_exec",
            new=process,
        ),
    ):
        result = await _handler()(
            {"source_group": "admin", "action": "click", "coordinate": [12, 34]}
        )

    assert result["result"]["output"] == {"ok": True}
    assert process.call_args.args[:5] == ("/bin/peekaboo", "click", "--coords", "12,34", "--json")


@pytest.mark.asyncio
async def test_peekaboo_set_value_requires_an_element_or_query() -> None:
    with (
        patch("pynchy.plugins.integrations.peekaboo.platform.system", return_value="Darwin"),
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value="/bin/peekaboo"),
    ):
        result = await _handler()(
            {"source_group": "admin", "action": "set_value", "value": "hello"}
        )

    assert "set_value requires element or query" in result["error"]
