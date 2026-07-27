"""Temporal activities for Pynchy host-side jobs."""

from __future__ import annotations

from pathlib import Path

from temporalio import activity

from pynchy.config import get_settings
from pynchy.config.jobs import (
    JobConfig,  # noqa: TC001, RUF100 - beartype resolves config host-job annotations at runtime.
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    _activity_workflow_id,
    _record_activity_result,
)
from pynchy.host.orchestrator.temporal.schedules import (
    is_stale_database_host_job_once_workflow,
)
from pynchy.logger import logger
from pynchy.state.api import get_host_job_by_id, record_host_job_completion
from pynchy.types import (
    HostJob,  # noqa: TC001, RUF100 - beartype resolves Temporal host-job annotations at runtime.
)
from pynchy.utils import ShellResult, log_shell_result, run_shell_command


def _resolve_job_cwd(cwd: str | None) -> str:
    project_root = get_settings().project_root
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

    try:
        await _run_database_host_job(job)
    except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; record activity failure.
        _record_activity_result(job_id, "error", str(exc))
        raise
    _record_activity_result(job_id, "completed")
    return "completed"


@activity.defn(name="run_config_host_cron_job")
async def run_config_host_cron_job(job_name: str) -> str:
    """Temporal activity that runs one enabled config-backed host cron job."""
    job = get_settings().jobs.get(job_name)
    if job is None or not job.is_host or not job.enabled:
        logger.info("Temporal config host cron job skipped", job=job_name)
        _record_activity_result(job_name, "skipped")
        return "skipped"

    try:
        await _run_config_host_cron_job(job_name, job)
    except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; record activity failure.
        _record_activity_result(job_name, "error", str(exc))
        raise
    _record_activity_result(job_name, "completed")
    return "completed"


async def _run_config_host_cron_job(job_name: str, job: JobConfig) -> None:
    """Run a config-backed host cron job and surface shell failures to Temporal."""
    if job.command is None or job.schedule is None:
        raise RuntimeError(f"validated host job {job_name!r} is incomplete")
    command_cwd = _resolve_job_cwd(job.cwd)
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
    )
    if not (job.quiet_on_success is True and result.returncode == 0):
        log_shell_result(result, label="Config host cron job", job=job_name)
    _raise_for_failed_command(result, job_name)


async def _run_database_host_job(job: HostJob) -> None:
    command_cwd = _resolve_job_cwd(job.cwd)
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
    )
    log_shell_result(result, label="Database host job", job_id=job.id)
    _raise_for_failed_command(result, job.id)

    await record_host_job_completion(job.id, completed=job.schedule_type == "once")


def _raise_for_failed_command(result: ShellResult, job_identifier: str) -> None:
    """Raise so Temporal records failed commands instead of false completion."""
    if result.start_error is not None:
        raise RuntimeError(f"Host job {job_identifier} failed to start: {result.start_error}")
    if result.timed_out:
        raise RuntimeError(f"Host job {job_identifier} timed out")
    if result.returncode != 0:
        raise RuntimeError(f"Host job {job_identifier} exited with code {result.returncode}")
