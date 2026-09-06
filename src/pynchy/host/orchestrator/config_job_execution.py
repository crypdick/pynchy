"""Host-side gates and deterministic output for workspace-owned config jobs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from pynchy.host.orchestrator.host_shell import ShellResult, run_shell_command
from pynchy.host.orchestrator.job_gates import parse_wake_agent_gate
from pynchy.scheduling.api import ScheduledTask

_MAX_PRE_RUN_STREAM_CHARS = 12_000


@runtime_checkable
class ConfigJobExecutionDeps(Protocol):
    """Runtime capabilities required by workspace-owned shell jobs."""

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DeterministicJobRun:
    """Scheduler bookkeeping returned by a handled deterministic job."""

    result: str
    error: str | None


def _bounded_pre_run_stream(value: str) -> str:
    if len(value) <= _MAX_PRE_RUN_STREAM_CHARS:
        return value
    marker = "\n... pre-run output truncated ...\n"
    side = (_MAX_PRE_RUN_STREAM_CHARS - len(marker)) // 2
    return f"{value[:side]}{marker}{value[-side:]}"


def _pre_run_prompt(task: ScheduledTask, result: ShellResult, command: str) -> str:
    parts = [
        task.prompt,
        f"\n\n--- Pre-run command: {command} ---",
        f"exit_code: {result.returncode}",
    ]
    if result.timed_out:
        parts.append("timed_out: true")
    if result.start_error is not None:
        parts.append(f"start_error: {result.start_error}")
    if result.stdout:
        parts.append(f"\nstdout:\n{_bounded_pre_run_stream(result.stdout)}")
    if result.stderr:
        parts.append(f"\nstderr:\n{_bounded_pre_run_stream(result.stderr)}")
    return "\n".join(parts)


def _job_env(automation_memory_dir: Path | None) -> dict[str, str]:
    env = {"PYNCHY_SCHEDULED_JOB": "1"}
    if automation_memory_dir is not None:
        env["PYNCHY_AUTOMATION_MEMORY_DIR"] = str(automation_memory_dir)
    return env


async def prepare_config_job(
    task: ScheduledTask,
    automation_memory_dir: Path | None = None,
) -> tuple[ScheduledTask | None, str | None]:
    """Run an agent config job's host gate before creating its thread."""
    if task.config_job_name is None:
        return task, None
    if task.config_job_pre_run_command is None:
        return task, None
    if task.config_job_pre_run_cwd is None:
        raise RuntimeError("Config job pre-run execution is incomplete")
    result = await run_shell_command(
        task.config_job_pre_run_command,
        cwd=task.config_job_pre_run_cwd,
        timeout_seconds=task.config_job_pre_run_timeout_seconds or 900,
        env=_job_env(automation_memory_dir),
    )
    if result.returncode == 0 and parse_wake_agent_gate(result.stdout) is False:
        return None, "Skipped: wakeAgent=false"
    return replace(
        task, prompt=_pre_run_prompt(task, result, task.config_job_pre_run_command)
    ), None


def _shell_result_error(result: ShellResult, job_name: str) -> str | None:
    if result.start_error is not None:
        return f"Workspace job {job_name} failed to start: {result.start_error}"
    if result.timed_out:
        return f"Workspace job {job_name} timed out"
    if result.returncode != 0:
        return f"Workspace job {job_name} exited with code {result.returncode}"
    return None


def _deterministic_job_output(job_name: str, result: ShellResult) -> str:
    chunks = [result.stdout] if result.stdout else []
    error = _shell_result_error(result, job_name)
    if result.stderr or error is not None:
        chunks.append(
            f"[Pynchy workspace job: {job_name}] exit_code={result.returncode}\n"
            f"{result.stderr or error or ''}".rstrip()
        )
    return "\n\n".join(chunk for chunk in chunks if chunk.strip())


async def run_deterministic_config_job(
    task: ScheduledTask,
    deps: ConfigJobExecutionDeps,
    automation_memory_dir: Path | None = None,
) -> DeterministicJobRun | None:
    """Run one workspace-owned host command without invoking an agent."""
    if task.config_job_name is None:
        return None
    if not task.config_job_is_deterministic:
        return None
    if task.config_job_command is None or task.config_job_cwd is None:
        raise RuntimeError("Deterministic config job execution is incomplete")
    result = await run_shell_command(
        task.config_job_command,
        cwd=task.config_job_cwd,
        timeout_seconds=task.config_job_timeout_seconds or 900,
        env=_job_env(automation_memory_dir),
    )
    if result.returncode == 0 and parse_wake_agent_gate(result.stdout) is False:
        return DeterministicJobRun(result="Skipped: wakeAgent=false", error=None)

    display_name = task.config_job_display_name or task.config_job_name
    output = _deterministic_job_output(display_name, result)
    if output:
        if task.bound_chat_jid is None:
            raise RuntimeError("Workspace job has no durable destination binding")
        await deps.broadcast_host_message(task.bound_chat_jid, output)
    return DeterministicJobRun(
        result=output or "Completed",
        error=_shell_result_error(result, display_name),
    )
