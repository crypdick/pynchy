"""Reconcile config-backed agent jobs into scheduled task rows."""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves job reconciliation annotations at runtime.
)
from dataclasses import dataclass, replace
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
from pynchy.host.orchestrator.workspace_placement import resolve_workspace_placement
from pynchy.logger import logger
from pynchy.state import create_task, get_task_by_id, rebind_task_root, resume_task, update_task
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves job reconciliation annotations at runtime.
    ScheduledTask,
    WorkspaceProfile,
)

_JOB_PROMPT_REQUIRED_ERROR = "agent job {job_name!r} requires prompt or prompt_file"
_JOB_SCHEDULE_REQUIRED_ERROR = "job {job_name!r} requires schedule or at"


def _job_task_id(job_name: str) -> str:
    # TODO: Preserve the exact config key in task identity; `foo_bar` and
    # `foo-bar` both normalize to the same task ID.
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
) -> tuple[Literal["cron", "interval", "once"], str]:
    job = settings.jobs[job_name]
    if job.schedule is not None:
        return "cron", job.schedule
    if job.interval_minutes is not None:
        return "interval", str(job.interval_minutes * 60 * 1000)
    at = job.at
    if at is None:
        raise ValueError(_JOB_SCHEDULE_REQUIRED_ERROR.format(job_name=job_name))
    return "once", at


@dataclass(frozen=True)
class _AgentJobContext:
    group: WorkspaceProfile
    resolved: ResolvedWorkspaceConfig
    root_folder: str


@dataclass(frozen=True)
class _AgentJobTaskDetails:
    prompt: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    context_mode: Literal["isolated"]
    derived_thread_name: str


async def _pause_disabled_job(task_id: str) -> None:
    existing = await get_task_by_id(task_id)
    if existing and existing.status == "active":
        await update_task(task_id, {"status": "paused"})


def _resolve_agent_job_context(
    job_name: str,
    folder_to_group: dict[str, WorkspaceProfile],
    resolve_config: Callable[[str], ResolvedWorkspaceConfig | None],
    root_folder: str,
) -> _AgentJobContext | None:
    placement = resolve_workspace_placement(folder_to_group.values(), root_folder)
    if placement is None:
        logger.warning(
            "Config job root workspace is not registered; skipping",
            job=job_name,
            workspace=root_folder,
        )
        return None

    resolved = resolve_config(root_folder)
    if resolved is None:
        logger.warning(
            "Config job root workspace has no resolved config; skipping",
            job=job_name,
            workspace=root_folder,
        )
        return None

    return _AgentJobContext(
        group=placement.owner,
        resolved=resolved,
        root_folder=root_folder,
    )


def _root_folder_for_job(job: JobConfig, settings: Settings) -> str | None:
    """Return an agent job's validated explicit parent workspace."""
    workspace = job.workspace
    return workspace if workspace is not None and settings.workspace_config(workspace) else None


async def _create_agent_job_task(
    *,
    task_id: str,
    job_name: str,
    group: WorkspaceProfile,
    root_folder: str,
    details: _AgentJobTaskDetails,
) -> None:
    await create_task(
        ScheduledTask(
            id=task_id,
            group_folder=root_folder,
            chat_jid=group.jid,
            prompt=details.prompt,
            schedule_type=details.schedule_type,
            schedule_value=details.schedule_value,
            context_mode=details.context_mode,
            status="active",
            created_at=datetime.now(UTC).isoformat(),
            config_job_name=job_name,
            derived_thread_name=details.derived_thread_name,
        )
    )


def _agent_job_updates(
    *,
    existing: ScheduledTask,
    job_name: str,
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
    if existing.context_mode != details.context_mode:
        updates["context_mode"] = details.context_mode
    if existing.repo_access is not None:
        updates["repo_access"] = None
    if existing.config_job_name != job_name:
        updates["config_job_name"] = job_name
    if existing.derived_thread_name != details.derived_thread_name:
        updates["derived_thread_name"] = details.derived_thread_name
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

        root_folder = _root_folder_for_job(job, settings)
        if root_folder is None:
            logger.warning(
                "Config job parent workspace is unavailable; skipping",
                job=job_name,
                workspace=job.workspace,
            )
            continue
        context = _resolve_agent_job_context(
            job_name,
            folder_to_group,
            resolve_config,
            root_folder,
        )
        if context is None:
            continue

        schedule_type, schedule_value = _job_schedule(job_name, settings)
        prompt = "" if job.is_deterministic else _job_prompt(job_name, settings)
        details = _AgentJobTaskDetails(
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            context_mode="isolated",
            derived_thread_name=(f"{context.root_folder} | {job.display_name or job_name}"),
        )
        desired_task_ids.add(task_id)
        existing = await get_task_by_id(task_id)

        if existing is None:
            await _create_agent_job_task(
                task_id=task_id,
                job_name=job_name,
                group=context.group,
                root_folder=context.root_folder,
                details=details,
            )
            logger.info("Created config agent job task", job=job_name, task_id=task_id)
            continue

        if existing.group_folder != context.root_folder:
            await rebind_task_root(
                task_id,
                group_folder=context.root_folder,
                chat_jid=context.group.jid,
            )
            existing = replace(
                existing,
                group_folder=context.root_folder,
                chat_jid=context.group.jid,
            )

        if existing.status != "active":
            await resume_task(task_id)
            existing = replace(existing, status="active")

        updates = _agent_job_updates(
            existing=existing,
            job_name=job_name,
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
