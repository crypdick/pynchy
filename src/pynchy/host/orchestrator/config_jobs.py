"""Reconcile config-backed agent jobs into scheduled task rows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pynchy.config.jobs import JobConfig
from pynchy.config.merge import ResolvedSandboxConfig
from pynchy.config.settings import Settings
from pynchy.logger import logger
from pynchy.state import create_task, get_task_by_id, update_task
from pynchy.types import ScheduledTask, WorkspaceProfile
from pynchy.utils import compute_next_run


def _job_task_id(job_name: str) -> str:
    return f"job-{job_name.replace('_', '-')}"


def _job_prompt(job_name: str, settings: Settings) -> str:
    job = settings.jobs[job_name]
    if job.prompt is not None:
        return job.prompt
    assert job.prompt_file is not None, "JobConfig validates agent jobs have prompt_file"
    path = settings.project_root / job.prompt_file
    return path.read_text()


def _job_schedule(
    job_name: str, settings: Settings
) -> tuple[Literal["cron", "once"], str, str | None]:
    job = settings.jobs[job_name]
    if job.schedule is not None:
        return "cron", job.schedule, compute_next_run("cron", job.schedule, settings.timezone)
    assert job.at is not None, "JobConfig validates jobs have schedule or at"
    return "once", job.at, job.at


@dataclass(frozen=True)
class _AgentJobContext:
    group: WorkspaceProfile
    resolved: ResolvedSandboxConfig


async def _pause_disabled_job(task_id: str) -> None:
    existing = await get_task_by_id(task_id)
    if existing and existing.status == "active":
        await update_task(task_id, {"status": "paused"})


def _resolve_agent_job_context(
    job_name: str,
    job: JobConfig,
    folder_to_group: dict[str, WorkspaceProfile],
    resolve_config: Callable[[str], ResolvedSandboxConfig | None],
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
    resolved: ResolvedSandboxConfig,
    prompt: str,
    schedule_type: Literal["cron", "once"],
    schedule_value: str,
    next_run: str | None,
    context_mode: Literal["group", "isolated"],
) -> None:
    await create_task(
        ScheduledTask(
            id=task_id,
            group_folder=job.workspace,
            chat_jid=group.jid,
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            context_mode=context_mode,
            repo_access=resolved.repo_access,
            next_run=next_run,
            status="active",
            created_at=datetime.now(UTC).isoformat(),
        )
    )


def _agent_job_updates(
    *,
    existing: ScheduledTask,
    group: WorkspaceProfile,
    resolved: ResolvedSandboxConfig,
    prompt: str,
    schedule_type: Literal["cron", "once"],
    schedule_value: str,
    next_run: str | None,
    context_mode: Literal["group", "isolated"],
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if existing.chat_jid != group.jid:
        updates["chat_jid"] = group.jid
    if existing.prompt != prompt:
        updates["prompt"] = prompt
    if existing.schedule_type != schedule_type:
        updates["schedule_type"] = schedule_type
    if existing.schedule_value != schedule_value:
        updates["schedule_value"] = schedule_value
        updates["next_run"] = next_run
    if existing.context_mode != context_mode:
        updates["context_mode"] = context_mode
    if existing.repo_access != resolved.repo_access:
        updates["repo_access"] = resolved.repo_access
    if existing.status != "active":
        updates["status"] = "active"
    return updates


async def reconcile_agent_jobs(
    workspaces: dict[str, WorkspaceProfile],
    settings: Settings,
    resolve_config: Callable[[str], ResolvedSandboxConfig | None],
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
        context_mode = cast(
            'Literal["group", "isolated"]',
            job.context_mode or context.resolved.context_mode,
        )
        desired_task_ids.add(task_id)
        existing = await get_task_by_id(task_id)

        if existing is None:
            await _create_agent_job_task(
                task_id=task_id,
                job=job,
                group=context.group,
                resolved=context.resolved,
                prompt=prompt,
                schedule_type=schedule_type,
                schedule_value=schedule_value,
                next_run=next_run,
                context_mode=context_mode,
            )
            logger.info("Created config agent job task", job=job_name, task_id=task_id)
            continue

        updates = _agent_job_updates(
            existing=existing,
            group=context.group,
            resolved=context.resolved,
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            next_run=next_run,
            context_mode=context_mode,
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
