"""Reconcile config-backed agent jobs into scheduled task rows."""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves job reconciliation annotations at runtime.
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pynchy.config.jobs import (
    JobConfig,  # noqa: TC001, RUF100 - beartype resolves job reconciliation annotations at runtime.
)
from pynchy.config.merge import (
    ResolvedWorkspaceConfig,  # noqa: TC001, RUF100 - beartype resolves job reconciliation annotations at runtime.
)
from pynchy.config.settings import (
    Settings,  # noqa: TC001, RUF100 - beartype resolves job reconciliation annotations at runtime.
)
from pynchy.logger import logger
from pynchy.state import create_task, get_task_by_id, update_task
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves job reconciliation annotations at runtime.
    ScheduledTask,
    WorkspaceProfile,
)
from pynchy.utils import compute_next_run

_JOB_PROMPT_REQUIRED_ERROR = "agent job {job_name!r} requires prompt or prompt_file"
_JOB_SCHEDULE_REQUIRED_ERROR = "job {job_name!r} requires schedule or at"


def _job_task_id(job_name: str) -> str:
    return f"job-{job_name.replace('_', '-')}"


def _job_prompt(job_name: str, settings: Settings) -> str:
    job = settings.jobs[job_name]
    if job.prompt is not None:
        return job.prompt
    prompt_file = job.prompt_file
    if prompt_file is None:
        raise ValueError(_JOB_PROMPT_REQUIRED_ERROR.format(job_name=job_name))
    path = settings.project_root / prompt_file
    return path.read_text()


def _job_schedule(
    job_name: str, settings: Settings
) -> tuple[Literal["cron", "once"], str, str | None]:
    job = settings.jobs[job_name]
    if job.schedule is not None:
        return "cron", job.schedule, compute_next_run("cron", job.schedule, settings.timezone)
    at = job.at
    if at is None:
        raise ValueError(_JOB_SCHEDULE_REQUIRED_ERROR.format(job_name=job_name))
    return "once", at, at


@dataclass(frozen=True)
class _AgentJobContext:
    group: WorkspaceProfile
    resolved: ResolvedWorkspaceConfig


@dataclass(frozen=True)
class _AgentJobTaskDetails:
    prompt: str
    schedule_type: Literal["cron", "once"]
    schedule_value: str
    next_run: str | None
    context_mode: Literal["isolated"]


async def _pause_disabled_job(task_id: str) -> None:
    existing = await get_task_by_id(task_id)
    if existing and existing.status == "active":
        await update_task(task_id, {"status": "paused"})


def _resolve_agent_job_context(
    job_name: str,
    job: JobConfig,
    folder_to_group: dict[str, WorkspaceProfile],
    resolve_config: Callable[[str], ResolvedWorkspaceConfig | None],
) -> _AgentJobContext | None:
    group = folder_to_group.get(job.workspace)
    if group is None:
        logger.warning(
            "Config job workspace is not registered; skipping",
            job=job_name,
            workspace=job.workspace,
        )
        return None

    resolved = resolve_config(job.workspace)
    if resolved is None:
        logger.warning(
            "Config job workspace has no resolved config; skipping",
            job=job_name,
            workspace=job.workspace,
        )
        return None

    return _AgentJobContext(group=group, resolved=resolved)


async def _create_agent_job_task(
    *,
    task_id: str,
    job: JobConfig,
    group: WorkspaceProfile,
    details: _AgentJobTaskDetails,
) -> None:
    await create_task(
        ScheduledTask(
            id=task_id,
            group_folder=job.workspace,
            chat_jid=group.jid,
            prompt=details.prompt,
            schedule_type=details.schedule_type,
            schedule_value=details.schedule_value,
            context_mode=details.context_mode,
            next_run=details.next_run,
            status="active",
            created_at=datetime.now(UTC).isoformat(),
        )
    )


def _agent_job_updates(
    *,
    existing: ScheduledTask,
    group: WorkspaceProfile,
    details: _AgentJobTaskDetails,
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if existing.chat_jid != group.jid:
        updates["chat_jid"] = group.jid
    if existing.prompt != details.prompt:
        updates["prompt"] = details.prompt
    if existing.schedule_type != details.schedule_type:
        updates["schedule_type"] = details.schedule_type
    if existing.schedule_value != details.schedule_value:
        updates["schedule_value"] = details.schedule_value
        updates["next_run"] = details.next_run
    if existing.context_mode != details.context_mode:
        updates["context_mode"] = details.context_mode
    if existing.repo_access is not None:
        updates["repo_access"] = None
    if existing.status != "active":
        updates["status"] = "active"
    return updates


async def reconcile_agent_jobs(
    workspaces: dict[str, WorkspaceProfile],
    settings: Settings,
    resolve_config: Callable[[str], ResolvedWorkspaceConfig | None],
) -> set[str]:
    """Create or update scheduled tasks declared under [jobs.*]."""
    desired_task_ids: set[str] = set()
    folder_to_group = {profile.folder: profile for profile in workspaces.values()}

    for job_name, job in settings.jobs.items():
        if job.is_host:
            continue
        task_id = _job_task_id(job_name)
        if not job.enabled:
            await _pause_disabled_job(task_id)
            continue

        context = _resolve_agent_job_context(job_name, job, folder_to_group, resolve_config)
        if context is None:
            continue

        schedule_type, schedule_value, next_run = _job_schedule(job_name, settings)
        prompt = _job_prompt(job_name, settings)
        details = _AgentJobTaskDetails(
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            next_run=next_run,
            context_mode="isolated",
        )
        desired_task_ids.add(task_id)
        existing = await get_task_by_id(task_id)

        if existing is None:
            await _create_agent_job_task(
                task_id=task_id,
                job=job,
                group=context.group,
                details=details,
            )
            logger.info("Created config agent job task", job=job_name, task_id=task_id)
            continue

        updates = _agent_job_updates(
            existing=existing,
            group=context.group,
            details=details,
        )
        if updates:
            await update_task(task_id, updates)
            logger.info(
                "Updated config agent job task",
                job=job_name,
                task_id=task_id,
                changed=list(updates.keys()),
            )

    return desired_task_ids
