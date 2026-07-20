"""Load plugin-owned jobs into Pynchy's native reconciliation paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from pynchy.config import get_settings
from pynchy.config.jobs import JobConfig
from pynchy.config.scheduler_models import CronJobConfig
from pynchy.logger import logger


@dataclass
class _PluginJobState:
    jobs: dict[str, JobConfig] = field(default_factory=dict)
    cron_jobs: dict[str, CronJobConfig] = field(default_factory=dict)


_state = _PluginJobState()


def _host_cron(job_name: str, job: JobConfig) -> CronJobConfig:
    if job.schedule is None or job.command is None:
        raise ValueError(f"plugin host job {job_name!r} requires schedule and command")
    return CronJobConfig(
        enabled=job.enabled,
        schedule=job.schedule,
        command=job.command,
        cwd=job.cwd,
        timeout_seconds=job.timeout_seconds or 600,
        quiet_on_success=job.quiet_on_success or False,
    )


def _validate_workspace_references(job_name: str, job: JobConfig) -> None:
    settings = get_settings()
    if not job.is_host and (
        job.workspace is None or settings.workspace_config(job.workspace) is None
    ):
        raise ValueError(f"plugin job {job_name!r} references unknown workspace: {job.workspace}")


def _clear_previous_contributions() -> None:
    settings = get_settings()
    for name, job in _state.jobs.items():
        if settings.jobs.get(name) == job:
            settings.jobs.pop(name)
    for name, cron_job in _state.cron_jobs.items():
        if settings.cron_jobs.get(name) == cron_job:
            settings.cron_jobs.pop(name)
    _state.jobs.clear()
    _state.cron_jobs.clear()


def _install_plugin_spec(spec: object, configured_names: set[str]) -> None:
    if not isinstance(spec, dict):
        logger.warning("Ignoring malformed plugin job spec", spec_type=type(spec).__name__)
        return
    raw_name = spec.get("name")
    config = spec.get("config")
    if not isinstance(raw_name, str) or not raw_name.strip() or not isinstance(config, dict):
        logger.warning("Ignoring malformed plugin job spec", spec=spec)
        return
    name = raw_name.strip()
    if name in configured_names:
        return
    try:
        job = JobConfig.model_validate(config)
        _validate_workspace_references(name, job)
    except (TypeError, ValueError) as exc:
        logger.warning("Ignoring invalid plugin job spec", job=name, err=str(exc))
        return
    settings = get_settings()
    settings.jobs[name] = job
    _state.jobs[name] = job
    configured_names.add(name)
    if job.is_host:
        cron_job = _host_cron(name, job)
        settings.cron_jobs[name] = cron_job
        _state.cron_jobs[name] = cron_job


def configure_plugin_jobs(plugin_manager: object | None) -> None:
    """Merge plugin job specs into native agent-task and host-cron config."""
    _clear_previous_contributions()
    settings = get_settings()
    if plugin_manager is None:
        return

    configured_names = set(settings.jobs)
    manager = cast("Any", plugin_manager)
    for contribution in manager.hook.pynchy_job_specs():
        if not isinstance(contribution, tuple | list):
            logger.warning(
                "Ignoring invalid plugin job collection",
                result_type=type(contribution).__name__,
            )
            continue
        for spec in contribution:
            _install_plugin_spec(spec, configured_names)
