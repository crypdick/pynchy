"""Reconcile config-backed agent jobs into scheduled task rows."""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves job reconciliation annotations at runtime.
)
from dataclasses import dataclass, replace
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


def _job_prompt(job_name: str, settings: Settings) -> str:
    job = settings.jobs[job_name]
    return cast("str", job.prompt)


def _job_schedule(
    job_name: str, settings: Settings
) -> tuple[Literal["cron", "interval", "once"], str]:
    job = settings.jobs[job_name]
    if job.schedule is not None:
        return "cron", job.schedule
    if job.interval_minutes is not None:
        return "interval", str(job.interval_minutes * 60 * 1000)
    return "once", cast("str", job.at)


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
    session_policy: SessionPolicy
    memory_enabled: bool
    derived_thread_name: str
    is_deterministic: bool
    command: str | None
    command_cwd: str | None
    command_timeout_seconds: int | None
    display_name: str | None
    pre_run_command: str | None
    pre_run_cwd: str | None
    pre_run_timeout_seconds: int | None


def _resolve_job_cwd(project_root: Path, cwd: str | None) -> str:
    if not cwd:
        return str(project_root)
    path = Path(cwd)
    return str(path if path.is_absolute() else (project_root / path).resolve())


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
            session_policy=details.session_policy,
            memory_enabled=details.memory_enabled,
            status="active",
            created_at=datetime.now(UTC).isoformat(),
            config_job_name=job_name,
            config_job_is_deterministic=details.is_deterministic,
            config_job_command=details.command,
            config_job_cwd=details.command_cwd,
            config_job_timeout_seconds=details.command_timeout_seconds,
            config_job_display_name=details.display_name,
            config_job_pre_run_command=details.pre_run_command,
            config_job_pre_run_cwd=details.pre_run_cwd,
            config_job_pre_run_timeout_seconds=details.pre_run_timeout_seconds,
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
    if existing.session_policy is not details.session_policy:
        updates["session_policy"] = details.session_policy
    if existing.memory_enabled is not details.memory_enabled:
        updates["memory_enabled"] = details.memory_enabled
    if existing.repo_access is not None:
        updates["repo_access"] = None
    if existing.config_job_name != job_name:
        updates["config_job_name"] = job_name
    execution_updates = {
        field: value
        for field, value in (
            ("config_job_is_deterministic", details.is_deterministic),
            ("config_job_command", details.command),
            ("config_job_cwd", details.command_cwd),
            ("config_job_timeout_seconds", details.command_timeout_seconds),
            ("config_job_display_name", details.display_name),
            ("config_job_pre_run_command", details.pre_run_command),
            ("config_job_pre_run_cwd", details.pre_run_cwd),
            ("config_job_pre_run_timeout_seconds", details.pre_run_timeout_seconds),
        )
        if getattr(existing, field) != value
    }
    updates.update(execution_updates)
    if existing.derived_thread_name != details.derived_thread_name:
        updates["derived_thread_name"] = details.derived_thread_name
    return updates


async def reconcile_agent_jobs(
    workspaces: dict[str, WorkspaceProfile],
    settings: Settings,
    resolve_config: Callable[[str], ResolvedWorkspaceConfig | None],
) -> set[str]:
    """Create or update scheduled tasks from configured automation jobs."""
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
            session_policy=(
                SessionPolicy.RESET_BEFORE_RUN if job.reset_before_run else SessionPolicy.CONTINUE
            ),
            memory_enabled=job.memory,
            derived_thread_name=f"⚙️ {job.display_name or job_name}",
            is_deterministic=job.is_deterministic,
            command=job.command if job.is_deterministic else None,
            command_cwd=(
                _resolve_job_cwd(settings.project_root, job.cwd) if job.is_deterministic else None
            ),
            command_timeout_seconds=job.timeout_seconds if job.is_deterministic else None,
            display_name=job.display_name,
            pre_run_command=job.pre_run_command,
            pre_run_cwd=(
                _resolve_job_cwd(settings.project_root, job.pre_run_cwd)
                if job.pre_run_command is not None
                else None
            ),
            pre_run_timeout_seconds=job.pre_run_timeout_seconds,
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
