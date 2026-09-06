"""IPC handler for group registration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # noqa: TC001 - beartype resolves group handler signatures at runtime.
)
from pynchy.host.container_manager.ipc.protocol import (
    RegisterGroupRequest,
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.security import cop_gate as cop_gate_module
from pynchy.logger import logger
from pynchy.workspace.api import WorkspaceProfile


async def _handle_register_group(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    if not is_admin:
        logger.warning(
            "Unauthorized register_group attempt blocked",
            source_group=source_group,
        )
        return

    request = RegisterGroupRequest.from_dict(data)
    if request is None:
        logger.warning(
            "Invalid register_group request - missing required fields",
            data=str(data),
        )
        return

    receipt = await cop_gate_module.verify_approval_receipt(
        "register_group", data, source_group, deps
    )
    if receipt is cop_gate_module.ReceiptVerification.INVALID:
        return
    if receipt is not cop_gate_module.ReceiptVerification.VALID:
        summary = f"name={request.name}, folder={request.folder}, trigger={request.trigger}"
        allowed = await cop_gate_module.cop_gate(
            "register_group",
            summary,
            data,
            source_group,
            deps,
        )
        if not allowed:
            return

    deps.register_workspace(
        WorkspaceProfile(
            jid=request.jid,
            name=request.name,
            folder=request.folder,
            trigger=request.trigger,
            added_at=datetime.now(UTC).isoformat(),
            container_config=request.container_config,
        ),
    )


register("register_group", _handle_register_group)
