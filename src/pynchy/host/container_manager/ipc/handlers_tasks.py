"""IPC handlers for task scheduling and lifecycle (pause/resume/cancel)."""

from __future__ import annotations

from collections.abc import (
    Awaitable,  # noqa: TC003 - beartype resolves task handler callbacks at runtime.
    Callable,  # noqa: TC003 - beartype resolves task handler callbacks at runtime.
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from croniter import croniter

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # beartype resolves task handler signatures at runtime.
    ScheduledWorkStore,
    TaskHandlerDeps,
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.security import cop_gate as cop_gate_module
from pynchy.logger import logger


@dataclass(frozen=True)
class _HostJobRequest:
    name: str
    command: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    cwd: str | None
    timeout_seconds: int


def _scheduled_work_store(deps: IpcDeps) -> ScheduledWorkStore:
    if not isinstance(deps, TaskHandlerDeps):
        raise TypeError("scheduled-work handlers require scheduled-work persistence")
    return deps.scheduled_work_store()


def _validate_schedule_from_ipc(
    schedule_type: Literal["cron", "interval", "once"],
    schedule_value: str,
) -> None:
    """Validate a persisted schedule definition without deriving its next fire time."""
    if schedule_type == "once":
        datetime.fromisoformat(schedule_value)
        return
    if schedule_type == "interval":
        if int(schedule_value) <= 0:
            raise ValueError("interval must be positive")
        return
    if not croniter.is_valid(schedule_value):
        raise ValueError("invalid cron expression")


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


def _required_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _schedule_type(value: object) -> Literal["cron", "interval", "once"] | None:
    if value in ("cron", "interval", "once"):
        return value
    return None


async def _handle_schedule_host_job(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    if not is_admin:
        logger.warning("Unauthorized schedule_host_job attempt", source_group=source_group)
        return

    receipt = await cop_gate_module.verify_approval_receipt(
        "schedule_host_job", data, source_group, deps
    )
    if receipt is cop_gate_module.ReceiptVerification.INVALID:
        return
    if receipt is not cop_gate_module.ReceiptVerification.VALID:
        summary = (
            f"name={data.get('name')}, "
            f"command={data.get('command')}, "
            f"schedule={data.get('schedule_type')}:{data.get('schedule_value')}"
        )
        allowed = await cop_gate_module.cop_gate(
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
        _validate_schedule_from_ipc(request.schedule_type, request.schedule_value)
    except (ValueError, TypeError, KeyError):
        logger.warning(
            "invalid schedule value for host job",
            schedule_type=request.schedule_type,
            schedule_value=request.schedule_value,
        )
        return
    job_id = f"host-{int(datetime.now(UTC).timestamp() * 1000)}-{uuid4().hex[:8]}"
    await _scheduled_work_store(deps).create_host_job(
        {
            "id": job_id,
            "name": request.name,
            "command": request.command,
            "schedule_type": request.schedule_type,
            "schedule_value": request.schedule_value,
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
    is_admin: bool,  # noqa: FBT001 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    store = _scheduled_work_store(deps)
    task_id = data.get("taskId", "")
    update = store.update_host_job if task_id.startswith("host-") else store.update_task
    await _authorized_task_action(
        data,
        source_group,
        is_admin=is_admin,
        action_name="pause",
        store=store,
        action=lambda tid: update(tid, {"status": "paused"}),
    )


async def _handle_resume_task(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    store = _scheduled_work_store(deps)
    task_id = data.get("taskId", "")
    action = (
        (lambda tid: store.update_host_job(tid, {"status": "active"}))
        if task_id.startswith("host-")
        else store.resume_task
    )
    await _authorized_task_action(
        data,
        source_group,
        is_admin=is_admin,
        action_name="resume",
        store=store,
        action=action,
    )


async def _handle_cancel_task(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    store = _scheduled_work_store(deps)
    task_id = data.get("taskId", "")
    action = store.delete_host_job if task_id.startswith("host-") else store.delete_task
    await _authorized_task_action(
        data,
        source_group,
        is_admin=is_admin,
        action_name="cancel",
        store=store,
        action=action,
    )


async def _authorized_task_action(  # noqa: PLR0913 - authorization needs task identity, actor, persistence, and operation.
    data: dict[str, Any],
    source_group: str,
    *,
    is_admin: bool,
    action_name: str,
    store: ScheduledWorkStore,
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

        job = await store.get_host_job_by_id(task_id)
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
        task = await store.get_task_by_id(task_id)
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


register("schedule_host_job", _handle_schedule_host_job)
register("pause_task", _handle_pause_task)
register("resume_task", _handle_resume_task)
register("cancel_task", _handle_cancel_task)
