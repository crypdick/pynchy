"""Read-only scheduled-work health projection for container agents."""

from __future__ import annotations

from typing import Any

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.logger import logger


async def _handle_task_status(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    """Project current scheduled-work health without exposing prompts or commands."""
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        logger.warning("Task status request missing request_id", source_group=source_group)
        return

    tasks, host_jobs = await deps.get_scheduled_work_status(
        source_group=source_group,
        is_admin=is_admin,
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
