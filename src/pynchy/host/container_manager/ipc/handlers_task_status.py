"""Read-only scheduled-work health projection for container agents."""

from __future__ import annotations

from typing import Any

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # noqa: TC001, RUF100 - beartype resolves registered handler signatures.
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.orchestrator.scheduled_work_status import collect_scheduled_work
from pynchy.host.orchestrator.temporal.status import get_temporal_orchestration_states
from pynchy.logger import logger
from pynchy.state import get_all_host_jobs, get_all_tasks, get_task_run_logs
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves reader return types at runtime.
    HostJob,
    ScheduledTask,
)


async def _visible_tasks(source_group: str, *, is_admin: bool) -> list[ScheduledTask]:
    tasks = await get_all_tasks()
    if is_admin:
        return tasks
    return [task for task in tasks if task.group_folder == source_group]


async def _visible_host_jobs(*, is_admin: bool) -> list[HostJob]:
    return await get_all_host_jobs() if is_admin else []


async def _handle_task_status(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001, RUF100 - registered handler callback keeps the IPC dispatch contract.
    _deps: IpcDeps,
) -> None:
    """Project current scheduled-work health without exposing prompts or commands."""
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        logger.warning("Task status request missing request_id", source_group=source_group)
        return

    tasks, host_jobs = await collect_scheduled_work(
        lambda: _visible_tasks(source_group, is_admin=is_admin),
        lambda: _visible_host_jobs(is_admin=is_admin),
        lambda task_id: get_task_run_logs(task_id, limit=5),
        get_temporal_orchestration_states,
    )
    write_ipc_response(
        ipc_response_path(source_group, request_id),
        {
            "result": {
                "tasks": tasks,
                "host_jobs": host_jobs,
                "coverage": {
                    "scope": "current Pynchy definitions and Temporal orchestration state",
                    "task_attempts": "latest result and five-attempt health summary",
                    "host_job_attempts": "Temporal orchestration state only",
                    "task_prompts_included": False,
                    "host_commands_included": False,
                },
            }
        },
    )


register("task_status", _handle_task_status)
