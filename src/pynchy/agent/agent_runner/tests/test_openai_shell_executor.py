"""Unit coverage for structured OpenAI shell execution results."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from unittest.mock import AsyncMock, Mock, patch

import pytest
from agents import ShellResult

from agent_runner.cores.openai_shell import make_shell_executor
from agent_runner.security.guard_git import guard_git_hook


def test_shell_executor_preserves_stderr_and_exit_code(tmp_path) -> None:
    executor = make_shell_executor(str(tmp_path))

    result = asyncio.run(
        executor({"action": {"commands": ["printf diagnostic >&2; exit 7"], "timeout_ms": 5_000}})
    )

    assert isinstance(result, ShellResult)
    assert len(result.output) == 1
    output = result.output[0]
    assert not output.stdout
    assert output.stderr == "diagnostic"
    assert output.exit_code == 7


def test_shell_executor_returns_one_output_per_command(tmp_path) -> None:
    executor = make_shell_executor(str(tmp_path))

    result = asyncio.run(
        executor({"action": {"commands": ["printf first", "printf second"], "timeout_ms": 5_000}})
    )

    assert isinstance(result, ShellResult)
    assert [output.stdout for output in result.output] == ["first", "second"]
    assert [output.exit_code for output in result.output] == [0, 0]


def test_shell_executor_carries_requested_output_limit(tmp_path) -> None:
    executor = make_shell_executor(str(tmp_path))

    result = asyncio.run(
        executor({"action": {"commands": ["printf diagnostic"], "max_output_length": 8}})
    )

    assert isinstance(result, ShellResult)
    assert result.max_output_length == 8


def test_shell_executor_reports_timed_out_command(tmp_path) -> None:
    executor = make_shell_executor(str(tmp_path))

    result = asyncio.run(executor({"action": {"commands": ["sleep 1"], "timeout_ms": 5}}))

    assert isinstance(result, ShellResult)
    assert result.output[0].outcome.type == "timeout"


def test_shell_executor_applies_one_timeout_to_the_command_sequence(tmp_path) -> None:
    executor = make_shell_executor(str(tmp_path))

    result = asyncio.run(
        executor({"action": {"commands": ["sleep 0.1", "sleep 0.1"], "timeout_ms": 150}})
    )

    assert isinstance(result, ShellResult)
    assert [output.outcome.type for output in result.output] == ["exit", "timeout"]


def test_shell_executor_timeout_kills_descendant_processes(tmp_path) -> None:
    pid_file = tmp_path / "child.pid"
    executor = make_shell_executor(str(tmp_path))

    result = asyncio.run(
        executor(
            {
                "action": {
                    "commands": [f"sleep 30 & echo $! > {pid_file}; wait"],
                    "timeout_ms": 100,
                }
            }
        )
    )

    pid = int(pid_file.read_text())
    try:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    assert result.output[0].outcome.type == "timeout"


def test_shell_executor_success_kills_background_descendants(tmp_path) -> None:
    pid_file = tmp_path / "child.pid"
    executor = make_shell_executor(str(tmp_path))

    result = asyncio.run(
        executor(
            {
                "action": {
                    "commands": [f"printf done; sleep 30 & echo $! > {pid_file}"],
                    "timeout_ms": 5_000,
                }
            }
        )
    )

    pid = int(pid_file.read_text())
    try:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    assert isinstance(result, ShellResult)
    assert result.output[0].stdout == "done"
    assert result.output[0].exit_code == 0


def test_shell_executor_cancellation_kills_descendant_processes(tmp_path) -> None:
    pid_file = tmp_path / "child.pid"
    executor = make_shell_executor(str(tmp_path))

    async def run_and_cancel() -> None:
        task = asyncio.create_task(
            executor({"action": {"commands": [f"sleep 30 & echo $! > {pid_file}; wait"]}})
        )
        await asyncio.sleep(0.05)
        assert pid_file.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())

    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_shell_cleanup_only_signals_owned_process_group(tmp_path) -> None:
    executor = make_shell_executor(str(tmp_path))
    process = AsyncMock()
    process.pid = 111
    process.kill = Mock()
    process.returncode = 0
    process.wait.return_value = 0

    with (
        patch(
            "agent_runner.cores.openai_shell.asyncio.create_subprocess_shell",
            new=AsyncMock(return_value=process),
        ),
        patch("agent_runner.cores.openai_shell.os.killpg") as killpg,
    ):
        result = asyncio.run(executor({"action": {"commands": ["printf safe"]}}))

    assert isinstance(result, ShellResult)
    killpg.assert_called_once_with(process.pid, signal.SIGKILL)
    process.kill.assert_called_once_with()


def test_shell_cleanup_refuses_synthetic_process_group_id(tmp_path) -> None:
    executor = make_shell_executor(str(tmp_path))
    process = AsyncMock()
    process.kill = Mock()
    process.returncode = 0
    process.wait.return_value = 0

    with (
        patch(
            "agent_runner.cores.openai_shell.asyncio.create_subprocess_shell",
            new=AsyncMock(return_value=process),
        ),
        patch("agent_runner.cores.openai_shell.os.killpg") as killpg,
    ):
        result = asyncio.run(executor({"action": {"commands": ["printf safe"]}}))

    assert isinstance(result, ShellResult)
    killpg.assert_not_called()
    process.kill.assert_called_once_with()


def test_shell_executor_cancellation_before_process_start_is_preserved(tmp_path) -> None:
    executor = make_shell_executor(str(tmp_path))
    started = asyncio.Event()

    async def blocked_spawn(*_args, **_kwargs):
        started.set()
        await asyncio.Future()

    async def run_and_cancel() -> None:
        with patch(
            "agent_runner.cores.openai_shell.asyncio.create_subprocess_shell",
            side_effect=blocked_spawn,
        ):
            task = asyncio.create_task(executor({"action": {"commands": ["echo never"]}}))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run_and_cancel())


def test_shell_executor_gates_each_executed_command(tmp_path) -> None:
    process = AsyncMock()
    process.kill = Mock()
    process.wait.return_value = 0
    process.returncode = 0
    executor = make_shell_executor(str(tmp_path), [guard_git_hook])

    with patch(
        "agent_runner.cores.openai_shell.asyncio.create_subprocess_shell",
        new=AsyncMock(return_value=process),
    ) as spawn:
        result = asyncio.run(
            executor(
                {
                    "action": {
                        "commands": ["printf safe #", "git push origin main"],
                        "timeout_ms": 5_000,
                    }
                }
            )
        )

    assert isinstance(result, ShellResult)
    assert spawn.await_count == 1
    assert result.output[-1].exit_code == 1
    assert "blocked by security policy" in result.output[-1].stderr.lower()
