"""IPC handlers for task scheduling and lifecycle (pause/resume/cancel)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pynchy.config import get_settings
from pynchy.db import (
    delete_host_job,
    delete_task,
    get_host_job_by_id,
    get_task_by_id,
    update_host_job,
    update_task,
)
from pynchy.ipc._deps import IpcDeps
from pynchy.ipc._registry import register
from pynchy.logger import logger
from pynchy.utils import compute_next_run


def _compute_next_run_from_ipc(
    schedule_type: str,
    schedule_value: str,
) -> str | None:
    """Compute next_run from IPC schedule data, returning None on invalid input.

    For 'once' tasks, parses the value as an ISO timestamp.
    For 'cron'/'interval', delegates to compute_next_run().
    """
    if schedule_type == "once":
        scheduled = datetime.fromisoformat(schedule_value)
        return scheduled.isoformat()

    return compute_next_run(schedule_type, schedule_value, get_settings().timezone)


async def _handle_schedule_task(
    data: dict[str, Any],
    source_group: str,
    is_god: bool,
    deps: IpcDeps,
) -> None:
    registered_groups = deps.registered_groups()

    prompt = data.get("prompt")
    schedule_type = data.get("schedule_type")
    schedule_value = data.get("schedule_value")
    target_jid = data.get("targetJid")

    if not (prompt and schedule_type and schedule_value and target_jid):
        return

    target_group_entry = registered_groups.get(target_jid)
    if not target_group_entry:
        logger.warning(
            "Cannot schedule task: target group not registered",
            target_jid=target_jid,
        )
        return

    target_folder = target_group_entry.folder

    if not is_god and target_folder != source_group:
        logger.warning(
            "Unauthorized schedule_task attempt blocked",
            source_group=source_group,
            target_folder=target_folder,
        )
        return

    try:
        next_run = _compute_next_run_from_ipc(schedule_type, schedule_value)
    except (ValueError, TypeError, KeyError):
        logger.warning(
            f"Invalid {schedule_type} value",
            schedule_value=schedule_value,
        )
        return

    task_id = f"task-{int(datetime.now(UTC).timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"
    context_mode = data.get("context_mode")
    if context_mode not in ("group", "isolated"):
        context_mode = "isolated"

    from pynchy.db import create_task

    await create_task(
        {
            "id": task_id,
            "group_folder": target_folder,
            "chat_jid": target_jid,
            "prompt": prompt,
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "context_mode": context_mode,
            "next_run": next_run,
            "status": "active",
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    logger.info(
        "Task created via IPC",
        task_id=task_id,
        source_group=source_group,
        target_folder=target_folder,
        context_mode=context_mode,
    )


async def _handle_schedule_host_job(
    data: dict[str, Any],
    source_group: str,
    is_god: bool,
    deps: IpcDeps,
) -> None:
    if not is_god:
        logger.warning("Unauthorized schedule_host_job attempt", source_group=source_group)
        return

    name = data.get("name")
    command = data.get("command")
    schedule_type = data.get("schedule_type")
    schedule_value = data.get("schedule_value")

    if not (name and command and schedule_type and schedule_value):
        logger.warning("Missing required fields for schedule_host_job", data=data)
        return

    try:
        next_run = _compute_next_run_from_ipc(schedule_type, schedule_value)
    except (ValueError, TypeError, KeyError):
        logger.warning(
            f"Invalid {schedule_type} value for host job",
            schedule_value=schedule_value,
        )
        return

    from pynchy.db import create_host_job

    job_id = f"host-{int(datetime.now(UTC).timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"
    await create_host_job(
        {
            "id": job_id,
            "name": name,
            "command": command,
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "next_run": next_run,
            "status": "active",
            "created_at": datetime.now(UTC).isoformat(),
            "created_by": source_group,
            "cwd": data.get("cwd"),
            "timeout_seconds": data.get("timeout_seconds", 600),
            "enabled": True,
        }
    )
    logger.info(
        "Host job created via IPC",
        job_id=job_id,
        name=name,
        source_group=source_group,
    )


async def _handle_pause_task(
    data: dict[str, Any],
    source_group: str,
    is_god: bool,
    deps: IpcDeps,
) -> None:
    task_id = data.get("taskId", "")
    _update = update_host_job if task_id.startswith("host-") else update_task
    await _authorized_task_action(
        data,
        source_group,
        is_god,
        "pause",
        lambda tid: _update(tid, {"status": "paused"}),
    )


async def _handle_resume_task(
    data: dict[str, Any],
    source_group: str,
    is_god: bool,
    deps: IpcDeps,
) -> None:
    task_id = data.get("taskId", "")
    _update = update_host_job if task_id.startswith("host-") else update_task
    await _authorized_task_action(
        data,
        source_group,
        is_god,
        "resume",
        lambda tid: _update(tid, {"status": "active"}),
    )


async def _handle_cancel_task(
    data: dict[str, Any],
    source_group: str,
    is_god: bool,
    deps: IpcDeps,
) -> None:
    task_id = data.get("taskId", "")
    action = delete_host_job if task_id.startswith("host-") else delete_task
    await _authorized_task_action(data, source_group, is_god, "cancel", action)


async def _authorized_task_action(
    data: dict[str, Any],
    source_group: str,
    is_god: bool,
    action_name: str,
    action: Callable[[str], Awaitable[Any]],
) -> None:
    """Fetch a task, verify authorization, and execute an action on it.

    Routes to the correct table based on ID prefix: host jobs use "host-"
    prefixed IDs and are god-only; agent tasks check group ownership.
    """
    task_id = data.get("taskId")
    if not task_id:
        return

    is_host_job = task_id.startswith("host-")

    if is_host_job:
        if not is_god:
            logger.warning(
                f"Unauthorized host job {action_name} attempt",
                task_id=task_id,
                source_group=source_group,
            )
            return

        job = await get_host_job_by_id(task_id)
        if job:
            await action(task_id)
            logger.info(
                f"Host job {action_name}d via IPC",
                task_id=task_id,
                source_group=source_group,
            )
        else:
            logger.warning("Host job not found", task_id=task_id)
    else:
        task = await get_task_by_id(task_id)
        if task and (is_god or task.group_folder == source_group):
            await action(task_id)
            logger.info(
                f"Task {action_name}d via IPC",
                task_id=task_id,
                source_group=source_group,
            )
        else:
            logger.warning(
                f"Unauthorized task {action_name} attempt",
                task_id=task_id,
                source_group=source_group,
            )


register("schedule_task", _handle_schedule_task)
register("schedule_host_job", _handle_schedule_host_job)
register("pause_task", _handle_pause_task)
register("resume_task", _handle_resume_task)
register("cancel_task", _handle_cancel_task)
