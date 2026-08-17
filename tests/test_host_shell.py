"""Tests for host shell process cleanup."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.host.orchestrator import host_shell
from pynchy.host.orchestrator.host_shell import ShellResult, log_shell_result, run_shell_command

if TYPE_CHECKING:
    from pathlib import Path


async def _wait_for_process_exit(pid: int) -> None:
    for _ in range(200):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"subprocess {pid} survived shell command cleanup")


def _pid_recording_command(pid_file: Path) -> str:
    script = (
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _exit_trap_command(pid_file: Path, cleanup_file: Path) -> str:
    command = _pid_recording_command(pid_file)
    cleanup = f"printf cleaned > {shlex.quote(str(cleanup_file))}"
    script = f"trap 'exit 143' TERM INT; trap {shlex.quote(cleanup)} EXIT; {command}"
    return f"/bin/sh -c {shlex.quote(script)}"


class _FakeProcess(asyncio.subprocess.Process):
    def __init__(self) -> None:
        self.terminate = MagicMock()
        self.kill = MagicMock()
        self.communicate = AsyncMock(return_value=(b"", b""))

    @property
    def pid(self) -> int:
        return 123

    @property
    def returncode(self) -> int | None:
        return None


async def _wait_for_pid_file(pid_file: Path) -> int:
    for _ in range(200):
        try:
            contents = await asyncio.to_thread(pid_file.read_text, encoding="utf-8")
        except FileNotFoundError:
            await asyncio.sleep(0.01)
            continue
        if contents:
            return int(contents)
        await asyncio.sleep(0.01)
    pytest.fail("subprocess did not record its PID")


class TestRunShellCommand:
    """Shell command cleanup must include every descendant process."""

    @pytest.mark.asyncio
    async def test_timeout_kills_descendant_process(self, tmp_path: Path):
        pid_file = tmp_path / "timeout-child.pid"
        result = await run_shell_command(
            _pid_recording_command(pid_file),
            cwd=str(tmp_path),
            timeout_seconds=1,
        )
        assert result.timed_out is True
        await _wait_for_process_exit(await _wait_for_pid_file(pid_file))

    @pytest.mark.asyncio
    async def test_timeout_allows_shell_exit_trap_to_clean_up(self, tmp_path: Path):
        pid_file = tmp_path / "trapped-child.pid"
        cleanup_file = tmp_path / "cleanup-complete"
        result = await run_shell_command(
            _exit_trap_command(pid_file, cleanup_file),
            cwd=str(tmp_path),
            timeout_seconds=1,
        )
        assert result.timed_out is True
        assert cleanup_file.read_text(encoding="utf-8") == "cleaned"
        await _wait_for_process_exit(await _wait_for_pid_file(pid_file))

    @pytest.mark.asyncio
    async def test_cancellation_kills_descendant_process(self, tmp_path: Path):
        pid_file = tmp_path / "cancelled-child.pid"
        command_task = asyncio.create_task(
            run_shell_command(
                _pid_recording_command(pid_file),
                cwd=str(tmp_path),
                timeout_seconds=60,
            )
        )
        child_pid = await _wait_for_pid_file(pid_file)
        command_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command_task
        await _wait_for_process_exit(child_pid)

    @pytest.mark.asyncio
    async def test_returns_completed_command_output(self, tmp_path: Path):
        result = await run_shell_command(
            "printf output; printf error >&2; exit 3",
            cwd=str(tmp_path),
            timeout_seconds=1,
        )

        assert result == ShellResult(returncode=3, stdout="output", stderr="error")

    @pytest.mark.asyncio
    async def test_returns_start_error_without_raising(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def fail_to_start(*_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(0)
            raise OSError("shell unavailable")

        monkeypatch.setattr(host_shell.asyncio, "create_subprocess_shell", fail_to_start)

        result = await run_shell_command("echo ignored", cwd=str(tmp_path))

        assert result.start_error == "shell unavailable"

    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_direct_kill_when_group_signal_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        process = _FakeProcess()

        def always_timeout(awaitable: object, timeout: float) -> object:
            del timeout
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise TimeoutError

        monkeypatch.setattr(
            host_shell.asyncio,
            "create_subprocess_shell",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(host_shell.asyncio, "wait_for", always_timeout)
        killpg = MagicMock(side_effect=PermissionError)
        monkeypatch.setattr(host_shell.os, "killpg", killpg)

        result = await run_shell_command("sleep 1", cwd=str(tmp_path), timeout_seconds=1)

        assert result.timed_out
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        assert [call.args[1] for call in killpg.call_args_list] == [
            host_shell.signal.SIGTERM,
            host_shell.signal.SIGKILL,
        ]

    @pytest.mark.asyncio
    async def test_timeout_tolerates_a_process_group_that_already_disappeared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        process = _FakeProcess()

        def always_timeout(awaitable: object, timeout: float) -> object:
            del timeout
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise TimeoutError

        monkeypatch.setattr(
            host_shell.asyncio,
            "create_subprocess_shell",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(host_shell.asyncio, "wait_for", always_timeout)
        killpg = MagicMock(side_effect=ProcessLookupError)
        monkeypatch.setattr(host_shell.os, "killpg", killpg)

        result = await run_shell_command("sleep 1", cwd=str(tmp_path), timeout_seconds=1)

        assert result.timed_out is True
        assert [call.args[1] for call in killpg.call_args_list] == [
            host_shell.signal.SIGTERM,
            host_shell.signal.SIGKILL,
        ]

    @pytest.mark.asyncio
    async def test_communication_failure_is_returned_after_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        process = _FakeProcess()
        wait_calls = 0

        async def fail_then_cleanup(awaitable: object, **kwargs: object) -> object:
            nonlocal wait_calls
            del kwargs
            wait_calls += 1
            if wait_calls == 1:
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
                raise RuntimeError("read failed")
            return await awaitable

        monkeypatch.setattr(
            host_shell.asyncio,
            "create_subprocess_shell",
            AsyncMock(return_value=process),
        )
        monkeypatch.setattr(host_shell.asyncio, "wait_for", fail_then_cleanup)
        monkeypatch.setattr(host_shell.os, "killpg", MagicMock())

        result = await run_shell_command("printf ignored", cwd=str(tmp_path))

        assert result == ShellResult(
            returncode=None,
            stdout="",
            stderr="",
            start_error="read failed",
        )


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (ShellResult(None, "", "", start_error="missing shell"), "failed to start"),
        (ShellResult(None, "", "", timed_out=True), "command timed out"),
        (ShellResult(0, "done", ""), "command completed"),
        (ShellResult(1, "", "failed"), "command failed"),
    ],
)
def test_log_shell_result_reports_each_outcome(
    caplog: pytest.LogCaptureFixture,
    result: ShellResult,
    message: str,
) -> None:
    with caplog.at_level("INFO"):
        log_shell_result(result, label="test", workspace="demo")

    assert message in caplog.text
