"""Temporal activities for Pynchy host-side jobs."""

from __future__ import annotations

from temporalio import activity

from pynchy.config import get_settings
from pynchy.host.orchestrator.task_scheduler import resolve_cron_job_cwd
from pynchy.host.orchestrator.temporal.runtime_state import _record_activity_result
from pynchy.logger import logger
from pynchy.state import get_host_job_by_id, update_host_job_after_run
from pynchy.types import (
    HostJob,  # noqa: TC001, RUF100 - beartype resolves Temporal host-job annotations at runtime.
)
from pynchy.utils import compute_next_run, log_shell_result, run_shell_command


@activity.defn(name="run_database_host_job")
async def run_database_host_job(job_id: str) -> str:
    """Temporal activity that runs one active database-backed host job."""
    job = await get_host_job_by_id(job_id)
    if job is None or job.status != "active" or not job.enabled:
        logger.info("Temporal database host job skipped", job_id=job_id)
        _record_activity_result(job_id, "skipped")
        return "skipped"

    try:
        await _run_database_host_job(job)
    except Exception as exc:  # allow: exception-handling - record activity failure
        _record_activity_result(job_id, "error", str(exc))
        raise
    _record_activity_result(job_id, "completed")
    return "completed"


@activity.defn(name="run_config_host_cron_job")
async def run_config_host_cron_job(job_name: str) -> str:
    """Temporal activity that runs one enabled config-backed host cron job."""
    job = get_settings().cron_jobs.get(job_name)
    if job is None or not job.enabled:
        logger.info("Temporal config host cron job skipped", job=job_name)
        _record_activity_result(job_name, "skipped")
        return "skipped"

    try:
        command_cwd = resolve_cron_job_cwd(job.cwd)
        logger.info(
            "Running config host cron job",
            job=job_name,
            schedule=job.schedule,
            cwd=command_cwd,
        )
        result = await run_shell_command(
            job.command,
            cwd=command_cwd,
            timeout_seconds=job.timeout_seconds,
        )
        if not (job.quiet_on_success and result.returncode == 0):
            log_shell_result(result, label="Config host cron job", job=job_name)
    except Exception as exc:  # allow: exception-handling - record activity failure
        _record_activity_result(job_name, "error", str(exc))
        raise
    _record_activity_result(job_name, "completed")
    return "completed"


async def _run_database_host_job(job: HostJob) -> None:
    command_cwd = resolve_cron_job_cwd(job.cwd)
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

    next_run = compute_next_run(job.schedule_type, job.schedule_value, get_settings().timezone)
    exit_code = result.returncode if result.returncode is not None else 1
    await update_host_job_after_run(job.id, next_run, exit_code)
