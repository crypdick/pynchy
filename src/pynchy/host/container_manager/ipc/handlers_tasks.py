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
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security import cop_gate as cop_gate_module
from pynchy.logger import logger
from pynchy.scheduling.api import (
    ScheduledTask,
)


@dataclass(frozen=True)
class _HostJobRequest:
    name: str
    command: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    cwd: str | None
    timeout_seconds: int
    memory_enabled: bool


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
    memory_enabled = data.get("memory", True)
    if not isinstance(memory_enabled, bool):
        return None

    return _HostJobRequest(
        name=name,
        command=command,
        schedule_type=schedule_type,
        schedule_value=schedule_value,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        memory_enabled=memory_enabled,
    )


def _required_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _schedule_type(value: object) -> Literal["cron", "interval", "once"] | None:
    if value in ("cron", "interval", "once"):
        return value
    return None


def _task_definition(task: ScheduledTask) -> dict[str, object]:
    """Project the small task definition an authorized agent may edit."""
    if not isinstance(task, ScheduledTask):
        raise TypeError("scheduled task store returned an invalid task")
    return {
        "id": task.id,
        "group": task.group_folder,
        "prompt": task.prompt,
        "schedule_type": task.schedule_type,
        "schedule_value": task.schedule_value,
        "session_policy": task.session_policy,
        "status": task.status,
        "memory_enabled": task.memory_enabled,
    }


def _request_id(data: dict[str, Any]) -> str | None:
    request_id = data.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else None


async def _authorized_editable_task(
    store: ScheduledWorkStore,
    task_id: object,
    source_group: str,
    *,
    is_admin: bool,
) -> ScheduledTask | None:
    if not isinstance(task_id, str) or not task_id or task_id.startswith("host-"):
        return None
    task = await store.get_task_by_id(task_id)
    if task is None or (not is_admin and task.group_folder != source_group):
        return None
    return task


def _write_task_response(source_group: str, request_id: str, task: ScheduledTask | None) -> None:
    response: dict[str, object]
    if task is None:
        response = {"error": "Scheduled task not found"}
    else:
        response = {"result": _task_definition(task)}
    write_ipc_response(ipc_response_path(source_group, request_id), response)


async def _handle_task_definition(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    request_id = _request_id(data)
    if request_id is None:
        return
    task = await _authorized_editable_task(
        _scheduled_work_store(deps), data.get("task_id"), source_group, is_admin=is_admin
    )
    _write_task_response(source_group, request_id, task)


def _task_update(data: dict[str, Any]) -> dict[str, str] | None:
    allowed = {"task_id", "prompt", "status", "request_id", "type"}
    if set(data) - allowed:
        return None
    updates: dict[str, str] = {}
    prompt = data.get("prompt")
    if prompt is not None:
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        updates["prompt"] = prompt
    status = data.get("status")
    if status is not None:
        if status not in {"active", "paused"}:
            return None
        updates["status"] = status
    return updates or None


async def _handle_update_scheduled_task(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    request_id = _request_id(data)
    updates = _task_update(data)
    if request_id is None or updates is None:
        if request_id is not None:
            write_ipc_response(
                ipc_response_path(source_group, request_id),
                {"error": "Invalid scheduled task update"},
            )
        return

    store = _scheduled_work_store(deps)
    task = await _authorized_editable_task(
        store, data.get("task_id"), source_group, is_admin=is_admin
    )
    if task is None:
        _write_task_response(source_group, request_id, None)
        return
    if task.config_job_name is not None:
        write_ipc_response(
            ipc_response_path(source_group, request_id),
            {"error": "Scheduled task is managed by its automation definition"},
        )
        return
    if "prompt" in updates:
        await store.update_task(task.id, {"prompt": updates["prompt"]})
    if updates.get("status") == "paused":
        await store.update_task(task.id, {"status": "paused"})
    elif updates.get("status") == "active" and task.status == "paused":
        await store.resume_task(task.id)
    reconciled = await store.get_task_by_id(task.id)
    _write_task_response(source_group, request_id, reconciled)


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
            "memory_enabled": request.memory_enabled,
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
    action = store.cancel_host_job if task_id.startswith("host-") else store.cancel_task
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
register("task_definition", _handle_task_definition)
register("update_scheduled_task", _handle_update_scheduled_task)
