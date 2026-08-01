"""Public backend availability and subprocess contract tests for Peekaboo."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins.api import ComputerUseRequest
from pynchy.plugins.integrations.peekaboo import PeekabooBackend, PeekabooConfig


class FakeProcess:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"{}", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True


def _request(action: str, **fields: object) -> ComputerUseRequest:
    return ComputerUseRequest.parse({"action": action, "source_group": "admin", **fields})


def test_availability_reports_platform_and_installation_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PeekabooBackend(PeekabooConfig(binary="peekaboo"))
    monkeypatch.setattr("pynchy.plugins.integrations.peekaboo.platform.system", lambda: "Linux")
    assert backend.availability().reason == "Peekaboo requires macOS"

    monkeypatch.setattr("pynchy.plugins.integrations.peekaboo.platform.system", lambda: "Darwin")
    monkeypatch.setattr("pynchy.plugins.integrations.peekaboo.shutil.which", lambda _name: None)
    assert backend.availability().reason == "Peekaboo is not installed at 'peekaboo'"

    monkeypatch.setattr(
        "pynchy.plugins.integrations.peekaboo.shutil.which", lambda _name: "/bin/peekaboo"
    )
    assert backend.availability().available is True


@pytest.mark.asyncio
async def test_execute_requires_binary_and_rejects_unimplemented_actions() -> None:
    backend = PeekabooBackend(PeekabooConfig(binary="peekaboo"))
    with (
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value=None),
        pytest.raises(RuntimeError, match="not installed"),
    ):
        await backend.execute(_request("list_apps"))

    with (
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value="/bin/peekaboo"),
        pytest.raises(ValueError, match="does not implement wait"),
    ):
        await backend.execute(_request("wait"))


@pytest.mark.asyncio
async def test_execute_without_screenshot_returns_provider_data_only() -> None:
    process = FakeProcess(stdout=json.dumps({"success": True, "data": {"ok": True}}).encode())
    backend = PeekabooBackend(PeekabooConfig(binary="peekaboo"))
    with (
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value="/bin/peekaboo"),
        patch(
            "pynchy.plugins.integrations.peekaboo.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    ):
        result = await backend.execute(_request("capture"))

    assert result == {
        "backend": "peekaboo",
        "peekaboo_action": "capture",
        "output": {"ok": True},
    }


@pytest.mark.asyncio
async def test_execute_accepts_string_keys_app_targets_and_delta_scrolls() -> None:
    backend = PeekabooBackend(PeekabooConfig(binary="peekaboo"))
    process = FakeProcess(stdout=b'{"success": true}')
    with (
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value="/bin/peekaboo"),
        patch(
            "pynchy.plugins.integrations.peekaboo.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as execute,
    ):
        await backend.execute(_request("launch_app", app="Safari"))
        await backend.execute(_request("key", keys="cmd+shift+p"))
        await backend.execute(_request("scroll", delta_y=-240))

    commands = [call.args for call in execute.call_args_list]
    assert commands[0][1:4] == ("app", "launch", "Safari")
    assert commands[1][1:5] == ("hotkey", "--keys", "cmd,shift,p", "--json")
    assert commands[2][1:5] == ("scroll", "--direction", "down", "--amount")


@pytest.mark.asyncio
async def test_execute_timeout_kills_provider_process() -> None:
    process = FakeProcess()
    backend = PeekabooBackend(PeekabooConfig(binary="peekaboo", timeout_seconds=2))

    def timeout_without_await(awaitable: object, _timeout: float) -> object:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    with (
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value="/bin/peekaboo"),
        patch(
            "pynchy.plugins.integrations.peekaboo.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
        patch(
            "pynchy.plugins.integrations.peekaboo.asyncio.wait_for",
            new=timeout_without_await,
        ),
        pytest.raises(RuntimeError, match="timed out after 2s"),
    ):
        await backend.execute(_request("list_apps"))

    assert process.killed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process", "error"),
    [
        (FakeProcess(returncode=2, stdout=b""), "Peekaboo failed: exit code 2"),
        (FakeProcess(stdout=b"not-json"), "Peekaboo returned invalid JSON: not-json"),
        (FakeProcess(stdout=b"[]"), "Peekaboo returned a non-object JSON response"),
        (
            FakeProcess(stdout=b'{"success": false, "error": "denied"}'),
            "Peekaboo failed: denied",
        ),
    ],
)
async def test_execute_reports_provider_response_failures(
    process: FakeProcess,
    error: str,
) -> None:
    backend = PeekabooBackend(PeekabooConfig(binary="peekaboo"))
    with (
        patch("pynchy.plugins.integrations.peekaboo.shutil.which", return_value="/bin/peekaboo"),
        patch(
            "pynchy.plugins.integrations.peekaboo.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
        pytest.raises((RuntimeError, TypeError), match=error),
    ):
        await backend.execute(_request("list_apps"))
