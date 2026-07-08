"""IPC handlers for task scheduling and lifecycle (pause/resume/cancel)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pynchy.config import get_settings
from pynchy.host.container_manager.ipc.deps import IpcDeps
from pynchy.host.container_manager.ipc.registry import register
from pynchy.logger import logger
from pynchy.state import (
    delete_host_job,
    delete_task,
    get_host_job_by_id,
    get_task_by_id,
    update_host_job,
    update_task,
)
from pynchy.types import GroupFolder, WorkspaceProfile
from pynchy.utils import compute_next_run


@dataclass(frozen=True)
class _ScheduledTaskRequest:
    prompt: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    target_folder: GroupFolder
    context_mode: Literal["group", "isolated"]


@dataclass(frozen=True)
class _HostJobRequest:
    name: str
    command: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    cwd: str | None
    timeout_seconds: int


def _compute_next_run_from_ipc(
    schedule_type: Literal["cron", "interval", "once"],
    schedule_value: str,
) -> str | None:
    """Compute next_run from IPC schedule data, returning None on invalid input.

    For 'once' tasks, parses the value as an ISO timestamp.
    For 'cron'/'interval', delegates to compute_next_run().
    """
    if schedule_type == "once":
        scheduled = datetime.fromisoformat(schedule_value)
        return scheduled.isoformat()

    # 'once' handled above; remaining values are validated by compute_next_run,
    # which returns None for anything other than 'cron'/'interval'.
    return compute_next_run(
        schedule_type,
        schedule_value,
        get_settings().timezone,
    )


async def _handle_schedule_task(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    if not data.get("_cop_approved"):
        from pynchy.host.container_manager.security.cop_gate import cop_gate

        prompt_preview = (data.get("prompt") or "")[:500]
        summary = f"target={data.get('targetGroup')}, schedule={data.get('schedule_type')}:{data.get('schedule_value')}, prompt={prompt_preview}"
        allowed = await cop_gate(
            "schedule_task",
            summary,
            data,
            source_group,
            deps,
        )
        if not allowed:
            return

    request = _scheduled_task_request(data)
    if request is None:
        return

    target_jid = _target_jid_for_folder(deps.workspaces(), request.target_folder)
    if target_jid is None:
        logger.warning(
            "Cannot schedule task: target group not registered",
            target_group=request.target_folder,
        )
        return

    if not is_admin and request.target_folder != source_group:
        logger.warning(
            "Unauthorized schedule_task attempt blocked",
            source_group=source_group,
            target_folder=request.target_folder,
        )
        return

    try:
        next_run = _compute_next_run_from_ipc(request.schedule_type, request.schedule_value)
    except (ValueError, TypeError, KeyError):
        logger.warning(
            "invalid schedule value",
            schedule_type=request.schedule_type,
            schedule_value=request.schedule_value,
        )
        return
    if next_run is None:
        logger.warning(
            "invalid schedule value",
            schedule_type=request.schedule_type,
            schedule_value=request.schedule_value,
        )
        return

    task_id = f"task-{int(datetime.now(UTC).timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"

    from pynchy.state import create_task
    from pynchy.types import ScheduledTask

    await create_task(
        ScheduledTask(
            id=task_id,
            group_folder=request.target_folder,
            chat_jid=target_jid,
            prompt=request.prompt,
            schedule_type=request.schedule_type,
            schedule_value=request.schedule_value,
            context_mode=request.context_mode,
            next_run=next_run,
            status="active",
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    logger.info(
        "Task created via IPC",
        task_id=task_id,
        source_group=source_group,
        target_folder=request.target_folder,
        context_mode=request.context_mode,
    )


def _scheduled_task_request(data: dict[str, Any]) -> _ScheduledTaskRequest | None:
    prompt = _required_str(data.get("prompt"))
    schedule_value = _required_str(data.get("schedule_value"))
    target_folder = _group_folder(data.get("targetGroup"))
    if prompt is None or schedule_value is None or target_folder is None:
        return None

    parsed_schedule_type = _schedule_type(data.get("schedule_type"))
    if parsed_schedule_type is None:
        return None

    return _ScheduledTaskRequest(
        prompt=prompt,
        schedule_type=parsed_schedule_type,
        schedule_value=schedule_value,
        target_folder=target_folder,
        context_mode=_context_mode(data.get("context_mode")),
    )


def _host_job_request(data: dict[str, Any]) -> _HostJobRequest | None:
    name = _required_str(data.get("name"))
    command = _required_str(data.get("command"))
    schedule_value = _required_str(data.get("schedule_value"))
    if name is None or command is None or schedule_value is None:
        return None

    schedule_type = _schedule_type(data.get("schedule_type"))
    if schedule_type is None:
        return None

    timeout_seconds = data.get("timeout_seconds", 600)
    if not isinstance(timeout_seconds, int):
        return None

    cwd = data.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return None

    return _HostJobRequest(
        name=name,
        command=command,
        schedule_type=schedule_type,
        schedule_value=schedule_value,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )


def _required_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _group_folder(value: Any) -> GroupFolder | None:
    parsed = _required_str(value)
    if parsed is None:
        return None
    return GroupFolder(parsed)


def _schedule_type(value: Any) -> Literal["cron", "interval", "once"] | None:
    if value in ("cron", "interval", "once"):
        return cast(Literal["cron", "interval", "once"], value)
    return None


def _context_mode(value: Any) -> Literal["group", "isolated"]:
    if value == "group":
        return "group"
    return "isolated"


def _target_jid_for_folder(
    workspaces: dict[str, WorkspaceProfile],
    target_folder: GroupFolder,
) -> str | None:
    for jid, profile in workspaces.items():
        if profile.folder == target_folder:
            return jid
    return None


async def _handle_schedule_host_job(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    if not is_admin:
        logger.warning("Unauthorized schedule_host_job attempt", source_group=source_group)
        return

    if not data.get("_cop_approved"):
        from pynchy.host.container_manager.security.cop_gate import cop_gate

        summary = f"name={data.get('name')}, command={data.get('command')}, schedule={data.get('schedule_type')}:{data.get('schedule_value')}"
        allowed = await cop_gate(
            "schedule_host_job",
            summary,
            data,
            source_group,
            deps,
        )
        if not allowed:
            return

    request = _host_job_request(data)
    if request is None:
        logger.warning("Missing required fields for schedule_host_job", data=data)
        return

    try:
        next_run = _compute_next_run_from_ipc(request.schedule_type, request.schedule_value)
    except (ValueError, TypeError, KeyError):
        logger.warning(
            "invalid schedule value for host job",
            schedule_type=request.schedule_type,
            schedule_value=request.schedule_value,
        )
        return
    if next_run is None:
        logger.warning(
            "invalid schedule value for host job",
            schedule_type=request.schedule_type,
            schedule_value=request.schedule_value,
        )
        return

    from pynchy.state import create_host_job

    job_id = f"host-{int(datetime.now(UTC).timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"
    await create_host_job(
        {
            "id": job_id,
            "name": request.name,
            "command": request.command,
            "schedule_type": request.schedule_type,
            "schedule_value": request.schedule_value,
            "next_run": next_run,
            "status": "active",
            "created_at": datetime.now(UTC).isoformat(),
            "created_by": source_group,
            "cwd": request.cwd,
            "timeout_seconds": request.timeout_seconds,
            "enabled": True,
        }
    )
    logger.info(
        "Host job created via IPC",
        job_id=job_id,
        name=request.name,
        source_group=source_group,
    )


async def _handle_pause_task(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    task_id = data.get("taskId", "")
    _update = update_host_job if task_id.startswith("host-") else update_task
    await _authorized_task_action(
        data,
        source_group,
        is_admin,
        "pause",
        lambda tid: _update(tid, {"status": "paused"}),
    )


async def _handle_resume_task(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    task_id = data.get("taskId", "")
    _update = update_host_job if task_id.startswith("host-") else update_task
    await _authorized_task_action(
        data,
        source_group,
        is_admin,
        "resume",
        lambda tid: _update(tid, {"status": "active"}),
    )


async def _handle_cancel_task(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    task_id = data.get("taskId", "")
    action = delete_host_job if task_id.startswith("host-") else delete_task
    await _authorized_task_action(data, source_group, is_admin, "cancel", action)


async def _authorized_task_action(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
    action_name: str,
    action: Callable[[str], Awaitable[Any]],
) -> None:
    """Fetch a task, verify authorization, and execute an action on it.

    Routes to the correct table based on ID prefix: host jobs use "host-"
    prefixed IDs and are admin-only; agent tasks check group ownership.
    """
    task_id = data.get("taskId")
    if not task_id:
        return

    is_host_job = task_id.startswith("host-")

    if is_host_job:
        if not is_admin:
            logger.warning(
                "unauthorized host job action attempt",
                action=action_name,
                task_id=task_id,
                source_group=source_group,
            )
            return

        job = await get_host_job_by_id(task_id)
        if job:
            await action(task_id)
            logger.info(
                "host job action via IPC",
                action=action_name,
                task_id=task_id,
                source_group=source_group,
            )
        else:
            logger.warning("Host job not found", task_id=task_id)
    else:
        task = await get_task_by_id(task_id)
        if task and (is_admin or task.group_folder == source_group):
            await action(task_id)
            logger.info(
                "task action via IPC",
                action=action_name,
                task_id=task_id,
                source_group=source_group,
            )
        else:
            logger.warning(
                "unauthorized task action attempt",
                action=action_name,
                task_id=task_id,
                source_group=source_group,
            )


register("schedule_task", _handle_schedule_task)
register("schedule_host_job", _handle_schedule_host_job)
register("pause_task", _handle_pause_task)
register("resume_task", _handle_resume_task)
register("cancel_task", _handle_cancel_task)
