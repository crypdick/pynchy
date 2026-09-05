"""Temporal activities for Pynchy host-side jobs."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from temporalio import activity

from pynchy.host.orchestrator.host_shell import ShellResult, log_shell_result, run_shell_command
from pynchy.host.orchestrator.scheduler_deps import (
    ConfigHostCronJob,  # noqa: TC001 - beartype resolves config host-job annotations at runtime.
    SchedulerDependencies,  # noqa: TC001 - beartype resolves host-job annotations at runtime.
    SchedulerRuntimeConfig,  # noqa: TC001 - beartype resolves host-job annotations at runtime.
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    _activity_workflow_id,
    _record_activity_result,
    _require_scheduler_deps,
)
from pynchy.host.orchestrator.temporal.schedules import (
    is_stale_database_host_job_once_workflow,
)
from pynchy.logger import logger
from pynchy.scheduling.api import (
    HostJob,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)
from pynchy.state.api import get_host_job_by_id, record_host_job_completion


@runtime_checkable  # noqa: V102
class _HostJobRuntime(Protocol):
    project_root: Path


@runtime_checkable
class _HostJobDeps(Protocol):
    scheduler_runtime: SchedulerRuntimeConfig


def _resolve_job_cwd(cwd: str | None, project_root: Path) -> str:
    if not cwd:
        return str(project_root)
    path = Path(cwd)
    return str(path if path.is_absolute() else (project_root / path).resolve())


@activity.defn(name="run_database_host_job")
async def run_database_host_job(job_id: str) -> str:
    """Temporal activity that runs one active database-backed host job."""
    job = await get_host_job_by_id(job_id)
    if job is None or job.status != "active" or not job.enabled:
        logger.info("Temporal database host job skipped", job_id=job_id)
        _record_activity_result(job_id, "skipped")
        return "skipped"
    activity_workflow_id = _activity_workflow_id()
    if activity_workflow_id is not None and is_stale_database_host_job_once_workflow(
        job, activity_workflow_id
    ):
        # The workflow ID versions delayed one-shot definitions. Reconciliation
        # cancels stale runs best-effort; this guard closes the due-time race.
        logger.info("Stale Temporal database host job skipped", job_id=job_id)
        _record_activity_result(job_id, "skipped")
        return "skipped"

    scheduler_deps = cast("SchedulerDependencies", _require_scheduler_deps())
    try:
        memory_context = (
            scheduler_deps.automation_memory_dir(job.id)
            if job.memory_enabled
            else nullcontext(None)
        )
        with memory_context as memory_dir:
            await _run_database_host_job(job, memory_dir, scheduler_deps)
    except Exception as exc:  # allow: exception-handling; record activity failure.
        _record_activity_result(job_id, "error", str(exc))
        raise
    _record_activity_result(job_id, "completed")
    return "completed"


@activity.defn(name="run_config_host_cron_job")
async def run_config_host_cron_job(job_name: str) -> str:
    """Temporal activity that runs one enabled config-backed host cron job."""
    scheduler_deps = cast("SchedulerDependencies", _require_scheduler_deps())
    job = scheduler_deps.scheduler_runtime.config_host_cron_jobs.get(job_name)
    if job is None:
        logger.info("Temporal config host cron job skipped", job=job_name)
        _record_activity_result(job_name, "skipped")
        return "skipped"

    try:
        memory_context = (
            scheduler_deps.automation_memory_dir(f"host-cron-{job_name}")
            if job.memory_enabled
            else nullcontext(None)
        )
        with memory_context as memory_dir:
            await _run_config_host_cron_job(
                job_name,
                job,
                scheduler_deps.scheduler_runtime.project_root,
                memory_dir,
            )
    except Exception as exc:  # allow: exception-handling; record activity failure.
        _record_activity_result(job_name, "error", str(exc))
        raise
    _record_activity_result(job_name, "completed")
    return "completed"


async def _run_config_host_cron_job(
    job_name: str,
    job: ConfigHostCronJob,
    project_root: Path,
    automation_memory_path: Path | None,
) -> None:
    """Run a config-backed host cron job and surface shell failures to Temporal."""
    command_cwd = _resolve_job_cwd(job.cwd, project_root)
    logger.info(
        "Running config host cron job",
        job=job_name,
        schedule=job.schedule,
        cwd=command_cwd,
    )
    result = await run_shell_command(
        job.command,
        cwd=command_cwd,
        timeout_seconds=job.timeout_seconds or 600,
        env=_automation_memory_env(automation_memory_path),
    )
    if not (job.quiet_on_success is True and result.returncode == 0):
        log_shell_result(result, label="Config host cron job", job=job_name)
    _raise_for_failed_command(result, job_name)


async def _run_database_host_job(
    job: HostJob,
    automation_memory_path: Path | None,
    scheduler_deps: _HostJobDeps,
) -> None:
    command_cwd = _resolve_job_cwd(job.cwd, scheduler_deps.scheduler_runtime.project_root)
    logger.info(
        "Running database host job",
        job_id=job.id,
        name=job.name,
        schedule_type=job.schedule_type,
        cwd=command_cwd,
    )

    result = await run_shell_command(
        job.command,
        cwd=command_cwd,
        timeout_seconds=job.timeout_seconds,
        env=_automation_memory_env(automation_memory_path),
    )
    log_shell_result(result, label="Database host job", job_id=job.id)
    _raise_for_failed_command(result, job.id)

    await record_host_job_completion(job.id, completed=job.schedule_type == "once")


def _automation_memory_env(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    return {"PYNCHY_AUTOMATION_MEMORY_DIR": str(path)}


def _raise_for_failed_command(result: ShellResult, job_identifier: str) -> None:
    """Raise so Temporal records failed commands instead of false completion."""
    if result.start_error is not None:
        raise RuntimeError(f"Host job {job_identifier} failed to start: {result.start_error}")
    if result.timed_out:
        raise RuntimeError(f"Host job {job_identifier} timed out")
    if result.returncode != 0:
        raise RuntimeError(f"Host job {job_identifier} exited with code {result.returncode}")
