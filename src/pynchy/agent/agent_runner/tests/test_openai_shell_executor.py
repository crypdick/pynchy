"""Unit coverage for structured OpenAI shell execution results."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

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


def test_shell_executor_gates_each_executed_command(tmp_path) -> None:
    process = AsyncMock()
    process.communicate.return_value = (b"safe", b"")
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
    spawn.assert_awaited_once_with(
        "printf safe #",
        cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert result.output[-1].exit_code == 1
    assert "blocked by security policy" in result.output[-1].stderr.lower()
