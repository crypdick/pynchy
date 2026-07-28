"""Tests for host shell process cleanup."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from typing import TYPE_CHECKING

import pytest

from pynchy.host.orchestrator.host_shell import run_shell_command

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


async def _wait_for_pid_file(pid_file: Path) -> int:
    for _ in range(200):
        try:
            contents = await asyncio.to_thread(pid_file.read_text, encoding="utf-8")
        except FileNotFoundError:
            await asyncio.sleep(0.01)
            continue
        return int(contents)
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
