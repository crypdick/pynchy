"""Host-side gates and deterministic output for workspace-owned config jobs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from pynchy.config import get_settings
from pynchy.config.workspace_names import dynamic_thread_folder
from pynchy.host.orchestrator.job_gates import parse_wake_agent_gate
from pynchy.host.orchestrator.threads import (  # noqa: TC001, RUF100 - beartype resolves execution dependency annotations.
    EnsuredThread,
)
from pynchy.host.orchestrator.workspace_placement import resolve_workspace_placement
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves execution annotations.
    ScheduledTask,
    WorkspaceProfile,
)
from pynchy.utils import ShellResult, run_shell_command

_MAX_PRE_RUN_STREAM_CHARS = 12_000


@runtime_checkable
class ConfigJobExecutionDeps(Protocol):
    """Runtime capabilities required by workspace-owned shell jobs."""

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def register_workspace(self, profile: WorkspaceProfile) -> None: ...

    async def ensure_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> EnsuredThread: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DeterministicJobRun:
    """Scheduler bookkeeping returned by a handled deterministic job."""

    result: str
    error: str | None


def derived_thread_name(task: ScheduledTask) -> str:
    """Return the configured human-readable child thread for owned work."""
    if task.derived_thread_name is not None:
        return task.derived_thread_name
    if task.config_job_name is not None:
        return f"{task.group_folder} | {task.config_job_name}"
    raise RuntimeError("Derived-thread task requires a thread name")


def resolve_job_cwd(cwd: str | None) -> str:
    """Resolve an optional job cwd against the Pynchy project root."""
    project_root = get_settings().project_root
    if not cwd:
        return str(project_root)
    path = Path(cwd)
    if path.is_absolute():
        return str(path)
    return str((project_root / path).resolve())


async def register_scheduled_target(
    deps: ConfigJobExecutionDeps,
    profile: WorkspaceProfile,
) -> None:
    """Persist an owned thread so later inbound replies retain its policy."""
    existing = deps.workspaces().get(profile.jid)
    if existing is not None:
        profile = replace(profile, added_at=existing.added_at)
    await deps.register_workspace(profile)


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


async def prepare_config_job(task: ScheduledTask) -> tuple[ScheduledTask | None, str | None]:
    """Run an agent config job's host gate before creating its thread."""
    if task.config_job_name is None:
        return task, None
    job = get_settings().jobs.get(task.config_job_name)
    if job is None or job.pre_run_command is None:
        return task, None
    result = await run_shell_command(
        job.pre_run_command,
        cwd=resolve_job_cwd(job.pre_run_cwd),
        timeout_seconds=job.pre_run_timeout_seconds or 900,
        env={"PYNCHY_SCHEDULED_JOB": "1"},
    )
    if result.returncode == 0 and parse_wake_agent_gate(result.stdout) is False:
        return None, "Skipped: wakeAgent=false"
    return replace(task, prompt=_pre_run_prompt(task, result, job.pre_run_command)), None


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
) -> DeterministicJobRun | None:
    """Run one workspace-owned host command without invoking an agent."""
    if task.config_job_name is None:
        return None
    job = get_settings().jobs.get(task.config_job_name)
    if job is None or not job.is_deterministic or job.command is None:
        return None
    result = await run_shell_command(
        job.command,
        cwd=resolve_job_cwd(job.cwd),
        timeout_seconds=job.timeout_seconds or 900,
        env={"PYNCHY_SCHEDULED_JOB": "1"},
    )
    if result.returncode == 0 and parse_wake_agent_gate(result.stdout) is False:
        return DeterministicJobRun(result="Skipped: wakeAgent=false", error=None)

    display_name = job.display_name or task.config_job_name
    output = _deterministic_job_output(display_name, result)
    if output:
        placement = resolve_workspace_placement(deps.workspaces().values(), task.group_folder)
        if placement is None:
            raise RuntimeError(f"Workspace placement not found: {task.group_folder}")
        thread_name = task.derived_thread_name or f"{task.group_folder} | {display_name}"
        ensured = await deps.ensure_thread(placement.control_parent.jid, thread_name)
        if ensured.jid is None:
            raise RuntimeError("Workspace job output thread returned no chat JID")
        await register_scheduled_target(
            deps,
            replace(
                placement.owner,
                jid=ensured.jid,
                name=f"{placement.owner.name}/{thread_name}",
                folder=dynamic_thread_folder(placement.owner.folder, ensured.jid),
            ),
        )
        await deps.broadcast_host_message(ensured.jid, output)
    return DeterministicJobRun(
        result=output or "Completed",
        error=_shell_result_error(result, display_name),
    )
