"""Payload-bound replay of approved host-mutating IPC requests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from pynchy.host.container_manager.ipc import registry
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security.identity import (
    guarded_action_id,
    issue_approval_receipt,
    revoke_approval_receipt,
)
from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.host.container_manager.ipc.deps import IpcDeps


async def execute_approved_ipc(
    request_data: dict[str, Any],
    source_group: str,
    request_id: str,
    operation: str,
    deps: object | None,
) -> None:
    """Dispatch one approved IPC request using a scoped, single-use receipt."""
    if deps is None:
        logger.error("Cannot dispatch IPC approval without deps", request_id=request_id)
        await asyncio.to_thread(
            write_ipc_response,
            ipc_response_path(source_group, request_id),
            {"error": "Internal error: IPC approval missing deps"},
        )
        return

    receipt = issue_approval_receipt(
        action_id=guarded_action_id(request_id),
        workspace=source_group,
        operation=operation,
        request_data=request_data,
    )
    try:
        request_data["_approval_receipt"] = str(receipt)
        await registry.dispatch(
            request_data, source_group, is_admin=True, deps=cast("IpcDeps", deps)
        )
        logger.info(
            "Approved IPC request dispatched",
            request_id=request_id,
            task_type=request_data.get("type"),
        )
    except Exception as exc:  # noqa: BLE001, RUF100 - approved IPC dispatch is an IPC boundary.
        logger.error("Approved IPC request failed", request_id=request_id, err=str(exc))
        await asyncio.to_thread(
            write_ipc_response,
            ipc_response_path(source_group, request_id),
            {"error": f"Execution failed: {exc}"},
        )
    finally:
        revoke_approval_receipt(receipt)
