"""Reconcile config-backed agent jobs into scheduled task rows."""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves job reconciliation annotations at runtime.
)
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pynchy.host.orchestrator.workspace_placement import resolve_workspace_placement
from pynchy.logger import logger
from pynchy.scheduling.api import (  # beartype resolves job reconciliation annotations at runtime.
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state.api import create_task, get_task_by_id, rebind_task_root, resume_task, update_task
from pynchy.workspace.api import (
    ResolvedWorkspaceConfig,  # noqa: TC001 - beartype resolves workspace policy annotations at runtime.
    WorkspaceProfile,  # noqa: TC001 - beartype resolves job reconciliation annotations at runtime.
)

type JobConfig = Any
type Settings = Any


def _job_task_id(job_name: str) -> str:
    # TODO: Preserve the exact config key in task identity; `foo_bar` and
    # `foo-bar` both normalize to the same task ID.
    return f"job-{job_name.replace('_', '-')}"


def _job_schedule(job: JobConfig) -> tuple[Literal["cron", "interval", "once"], str]:
    if job.schedule is not None:
        return "cron", job.schedule
    if job.interval_minutes is not None:
        return "interval", str(job.interval_minutes * 60 * 1000)
    return "once", cast("str", job.at)


def _resolve_job_cwd(project_root: Path, cwd: str | None) -> str:
    if not cwd:
        return str(project_root)
    path = Path(cwd)
    return str(path if path.is_absolute() else (project_root / path).resolve())


async def _pause_disabled_job(task_id: str) -> None:
    existing = await get_task_by_id(task_id)
    if existing and existing.status == "active":
        await update_task(task_id, {"status": "paused"})


def _resolve_job_owner(
    job_name: str,
    workspaces: dict[str, WorkspaceProfile],
    resolve_config: Callable[[str], ResolvedWorkspaceConfig | None],
    root_folder: str,
) -> WorkspaceProfile | None:
    placement = resolve_workspace_placement(workspaces.values(), root_folder)
    if placement is None:
        logger.warning(
            "Config job root workspace is not registered; skipping",
            job=job_name,
            workspace=root_folder,
        )
        return None

    if resolve_config(root_folder) is None:
        logger.warning(
            "Config job root workspace has no resolved config; skipping",
            job=job_name,
            workspace=root_folder,
        )
        return None

    return placement.owner


def _root_folder_for_job(job: JobConfig, settings: Settings) -> str | None:
    """Return an agent job's validated explicit parent workspace."""
    workspace = job.workspace
    return workspace if workspace is not None and settings.workspace_config(workspace) else None


def _agent_job_updates(existing: ScheduledTask, desired: ScheduledTask) -> dict[str, Any]:
    # Config owns these fields. Preserve execution history and thread bindings;
    # root rebinding and resuming use their dedicated state operations below.
    return {
        field: getattr(desired, field)
        for field in (
            "chat_jid",
            "prompt",
            "schedule_type",
            "schedule_value",
            "session_policy",
            "memory_enabled",
            "repo_access",
            "config_job_name",
            "config_job_is_deterministic",
            "config_job_command",
            "config_job_cwd",
            "config_job_timeout_seconds",
            "config_job_display_name",
            "config_job_pre_run_command",
            "config_job_pre_run_cwd",
            "config_job_pre_run_timeout_seconds",
            "derived_thread_name",
        )
        if getattr(existing, field) != getattr(desired, field)
    }


async def reconcile_agent_jobs(
    workspaces: dict[str, WorkspaceProfile],
    settings: Settings,
    resolve_config: Callable[[str], ResolvedWorkspaceConfig | None],
) -> set[str]:
    """Create or update scheduled tasks from configured automation jobs."""
    desired_task_ids: set[str] = set()

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
        group = _resolve_job_owner(
            job_name,
            workspaces,
            resolve_config,
            root_folder,
        )
        if group is None:
            continue

        schedule_type, schedule_value = _job_schedule(job)
        desired = ScheduledTask(
            id=task_id,
            group_folder=root_folder,
            chat_jid=group.jid,
            prompt="" if job.is_deterministic else cast("str", job.prompt),
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            session_policy=(
                SessionPolicy.RESET_BEFORE_RUN if job.reset_before_run else SessionPolicy.CONTINUE
            ),
            memory_enabled=job.memory,
            derived_thread_name=f"⚙️ {job.display_name or job_name}",
            created_at=datetime.now(UTC).isoformat(),
            config_job_name=job_name,
            config_job_is_deterministic=job.is_deterministic,
            config_job_command=job.command if job.is_deterministic else None,
            config_job_cwd=(
                _resolve_job_cwd(settings.project_root, job.cwd) if job.is_deterministic else None
            ),
            config_job_timeout_seconds=job.timeout_seconds if job.is_deterministic else None,
            config_job_display_name=job.display_name,
            config_job_pre_run_command=job.pre_run_command,
            config_job_pre_run_cwd=(
                _resolve_job_cwd(settings.project_root, job.pre_run_cwd)
                if job.pre_run_command is not None
                else None
            ),
            config_job_pre_run_timeout_seconds=job.pre_run_timeout_seconds,
        )
        desired_task_ids.add(task_id)
        existing = await get_task_by_id(task_id)

        if existing is None:
            await create_task(desired)
            logger.info("Created config agent job task", job=job_name, task_id=task_id)
            continue

        if existing.group_folder != root_folder:
            await rebind_task_root(
                task_id,
                group_folder=root_folder,
                chat_jid=group.jid,
            )
            existing = replace(
                existing,
                group_folder=root_folder,
                chat_jid=group.jid,
            )

        if existing.status != "active":
            await resume_task(task_id)

        updates = _agent_job_updates(existing, desired)
        if updates:
            await update_task(task_id, updates)
            logger.info(
                "Updated config agent job task",
                job=job_name,
                task_id=task_id,
                changed=list(updates.keys()),
            )

    return desired_task_ids
