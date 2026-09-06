"""Load plugin-owned jobs into Pynchy's native reconciliation paths."""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves plugin-job runtime annotations.
)
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from pynchy.logger import logger
from pynchy.plugins.api import JobSpec

type JobConfig = Any


def _unconfigured_runtime(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("Plugin job configuration has not been composed")


@dataclass(frozen=True)
class PluginJobsRuntime:
    get_settings: Callable[[], Any]
    parse_job: Callable[[dict[str, object]], Any]


_runtime = PluginJobsRuntime(
    get_settings=_unconfigured_runtime,
    parse_job=_unconfigured_runtime,
)


def configure_plugin_jobs_runtime(runtime: PluginJobsRuntime) -> None:
    """Bind plugin-job validation and settings mutation at composition."""
    global _runtime  # noqa: PLW0603 - one host process owns plugin job configuration.
    _runtime = runtime


def get_settings() -> object:
    return _runtime.get_settings()


_plugin_jobs: dict[str, JobConfig] = {}


def _validate_workspace_references(job_name: str, job: JobConfig) -> None:
    settings = cast("Any", get_settings())
    if not job.is_host and (
        job.workspace is None or settings.workspace_config(job.workspace) is None
    ):
        raise ValueError(f"plugin job {job_name!r} references unknown workspace: {job.workspace}")


def _clear_previous_contributions() -> None:
    settings = cast("Any", get_settings())
    for name, job in _plugin_jobs.items():
        if settings.jobs.get(name) == job:
            settings.jobs.pop(name)
    _plugin_jobs.clear()


def _install_plugin_spec(spec: object, configured_names: set[str]) -> None:
    if not isinstance(spec, JobSpec):
        logger.warning("Ignoring malformed plugin job spec", spec_type=type(spec).__name__)
        return
    name = spec.name.strip()
    if not name:
        logger.warning("Ignoring unnamed plugin job spec")
        return
    if name in configured_names:
        return
    try:
        job = _runtime.parse_job(spec.config)
        _validate_workspace_references(name, job)
    except (TypeError, ValueError) as exc:
        logger.warning("Ignoring invalid plugin job spec", job=name, err=str(exc))
        return
    settings = cast("Any", get_settings())
    settings.jobs[name] = job
    _plugin_jobs[name] = job
    configured_names.add(name)


def configure_plugin_jobs(plugin_manager: object | None) -> None:
    """Merge plugin job specs into native agent-task and host-cron config."""
    _clear_previous_contributions()
    settings = cast("Any", get_settings())
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
