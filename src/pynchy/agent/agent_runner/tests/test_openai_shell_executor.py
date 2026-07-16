"""Unit coverage for structured OpenAI shell execution results."""

from __future__ import annotations

import asyncio

from agents import ShellResult

from agent_runner.cores.openai_shell import make_shell_executor


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
