"""Local execution adapter for the OpenAI Responses shell tool."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents import ShellCallOutcome, ShellCommandOutput, ShellResult

_PROCESS_EXIT_GRACE_SECONDS = 0.1

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agent_runner.hooks import BeforeToolUseHook


def make_shell_executor(
    cwd: str,
    before_tool_hooks: list[BeforeToolUseHook] | None = None,
) -> Callable[[Any], Awaitable[str | ShellResult]]:
    """Create a structured shell executor bound to one container working directory."""

    async def executor(request: object) -> str | ShellResult:
        data = _field(request, "data")
        action = _field(data, "action") or _field(request, "action")
        command_list = _commands(action)
        if not command_list:
            return _failure_result("Shell tool request missing commands.")

        command = " && ".join(command_list)
        raw_timeout_ms = _field(action, "timeout_ms") or _field(data, "timeout_ms")
        timeout_ms = raw_timeout_ms if isinstance(raw_timeout_ms, int | float) else 120_000
        timeout_s = timeout_ms / 1000
        max_output_length = _field(action, "max_output_length") or _field(data, "max_output_length")

        _log(f"Shell ({cwd}): {command[:200]}")

        results: list[ShellCommandOutput] = []
        deadline = asyncio.get_running_loop().time() + timeout_s
        for shell_command in command_list:
            proc: asyncio.subprocess.Process | None = None
            try:  # noqa: PLW0717 - one cleanup boundary must retain the spawned process.
                if blocked := await asyncio.wait_for(
                    _blocked_message(before_tool_hooks, shell_command),
                    timeout=max(0, deadline - asyncio.get_running_loop().time()),
                ):
                    results.extend(_failure_result(blocked).output)
                    break
                with (
                    tempfile.TemporaryFile() as stdout_file,
                    tempfile.TemporaryFile() as stderr_file,
                ):
                    proc = await asyncio.wait_for(
                        asyncio.create_subprocess_shell(
                            shell_command,
                            cwd=cwd,
                            stdout=stdout_file,
                            stderr=stderr_file,
                            start_new_session=True,
                        ),
                        timeout=max(0, deadline - asyncio.get_running_loop().time()),
                    )
                    await asyncio.wait_for(
                        proc.wait(),
                        timeout=max(0, deadline - asyncio.get_running_loop().time()),
                    )
                    await _kill_process_group(proc)
                    stdout_file.seek(0)
                    stderr_file.seek(0)
                    stdout, stderr = stdout_file.read(), stderr_file.read()
            except TimeoutError:
                if proc is not None:
                    await _kill_process_group(proc)
                results.append(
                    ShellCommandOutput(
                        stderr=f"Command timed out after {timeout_s}s",
                        outcome=ShellCallOutcome(type="timeout"),
                        command=shell_command,
                    )
                )
                break
            except asyncio.CancelledError:
                if proc is not None:
                    await _kill_process_group(proc)
                raise
            except Exception as exc:  # allow: exception-handling; tool error.  # noqa: BLE001
                results.append(
                    ShellCommandOutput(
                        stderr=f"Shell error: {exc}",
                        outcome=ShellCallOutcome(type="exit", exit_code=1),
                        command=shell_command,
                    )
                )
                break
            else:
                results.append(
                    ShellCommandOutput(
                        stdout=stdout.decode(errors="replace"),
                        stderr=stderr.decode(errors="replace"),
                        outcome=ShellCallOutcome(type="exit", exit_code=proc.returncode),
                        command=shell_command,
                    )
                )
                if proc.returncode:
                    break

        output_limit = (
            max_output_length
            if isinstance(max_output_length, int) and max_output_length > 0
            else None
        )
        return ShellResult(output=results, max_output_length=output_limit)

    return executor


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    descendants = _process_group_pids(proc.pid)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()
    await _wait_for_process_exit(descendants)


async def _wait_for_process_exit(pids: list[int]) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PROCESS_EXIT_GRACE_SECONDS
    while any(
        Path(f"/proc/{pid}").exists()  # noqa: ASYNC240 - bounded local process metadata read.
        for pid in pids
    ):
        if loop.time() >= deadline:
            return
        await asyncio.sleep(0.005)


def _process_group_pids(process_group: int) -> list[int]:
    members: list[int] = []
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        with contextlib.suppress(OSError, ValueError):
            fields = stat_path.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == process_group:
                members.append(int(stat_path.parent.name))
    return members


def _field(obj: object, name: str) -> object | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _commands(action: object) -> list[str]:
    commands = _field(action, "commands")
    if commands is None:
        command = _field(action, "command")
        commands = [command] if command else None
    if isinstance(commands, list | tuple):
        return [str(command) for command in commands]
    return [str(commands)] if commands else []


async def _blocked_message(
    before_tool_hooks: list[BeforeToolUseHook] | None, command: str
) -> str | None:
    if not before_tool_hooks:
        return None
    for hook_fn in before_tool_hooks:
        decision = await hook_fn("Bash", {"command": command})
        if not decision.allowed:
            _log(f"Command blocked by hook: {decision.reason}")
            return f"Command blocked by security policy: {decision.reason}"
    return None


def _failure_result(message: str) -> ShellResult:
    """Make non-executed shell requests visible as structured failed outcomes."""
    return ShellResult(
        output=[
            ShellCommandOutput(
                stderr=message,
                outcome=ShellCallOutcome(type="exit", exit_code=1),
            )
        ]
    )


def _log(message: str) -> None:
    """Log through the existing OpenAI-core container stderr channel."""
    sys.stderr.write(f"[openai-core] {message}\n")
    sys.stderr.flush()
