"""IPC handler for persistent learned-skill access decisions."""

from __future__ import annotations

from typing import Any, cast

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.logger import logger


def _write_result(source_group: str, request_id: str, result: dict[str, object]) -> None:
    write_ipc_response(ipc_response_path(source_group, request_id), {"result": result})


async def _handle_skill_access(  # noqa: RUF029 - registered async IPC handler contract.
    data: dict[str, Any],
    source_group: str,
    _is_admin: bool,  # noqa: FBT001 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    request_id = data.get("request_id")
    action = data.get("action")
    skill_name = data.get("skill_name")
    if not all(isinstance(value, str) for value in (request_id, action, skill_name)):
        logger.warning("Malformed skill access request", source_group=source_group)
        return
    request_id = cast("str", request_id)
    action = cast("str", action)
    skill_name = cast("str", skill_name)

    status = deps.skill_access_status(source_group, skill_name)
    if status == "unknown":
        _write_result(source_group, request_id, {"status": "unknown", "skill_name": skill_name})
        return

    if action == "status":
        _write_result(
            source_group,
            request_id,
            {"status": status, "skill_name": skill_name},
        )
        return

    if action not in {"grant_always", "deny_always"}:
        _write_result(
            source_group,
            request_id,
            {"status": "error", "message": f"Unknown skill access action: {action}"},
        )
        return

    _write_result(
        source_group,
        request_id,
        {
            "status": "error",
            "message": "Persistent skill decisions must be completed by an ask_user response.",
        },
    )


register("skill_access:policy", _handle_skill_access)
