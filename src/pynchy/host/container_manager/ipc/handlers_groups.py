"""IPC handlers for group registration, refresh, and periodic agent creation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from croniter import croniter

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # noqa: TC001, RUF100 - beartype resolves group handler signatures at runtime.
)
from pynchy.host.container_manager.ipc.protocol import (
    CreatePeriodicAgentRequest,
    RegisterGroupRequest,
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.security import cop_gate as cop_gate_module
from pynchy.logger import logger
from pynchy.workspace.api import WorkspaceProfile


async def _handle_register_group(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001, RUF100 - registered handler callback keeps the IPC dispatch contract.
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


async def _handle_create_periodic_agent(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001, RUF100 - registered handler callback keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    """Create a periodic agent: folder, personalized workspace, instructions, and task."""
    request = _periodic_agent_request(data, source_group=source_group, is_admin=is_admin)
    if request is None:
        return

    if not await _periodic_agent_cop_allowed(request, data, source_group, deps):
        return

    await deps.create_periodic_agent(request)


def _periodic_agent_request(
    data: dict[str, Any], *, source_group: str, is_admin: bool
) -> CreatePeriodicAgentRequest | None:
    if not is_admin:
        logger.warning(
            "Unauthorized create_periodic_agent attempt blocked",
            source_group=source_group,
        )
        return None

    request = CreatePeriodicAgentRequest.from_dict(data)
    if request is None:
        logger.warning("create_periodic_agent missing required fields", data=str(data))
        return None

    if not croniter.is_valid(request.schedule):
        logger.warning("create_periodic_agent invalid cron", schedule=request.schedule)
        return None
    return request


async def _periodic_agent_cop_allowed(
    request: CreatePeriodicAgentRequest,
    data: dict[str, Any],
    source_group: str,
    deps: IpcDeps,
) -> bool:
    receipt = await cop_gate_module.verify_approval_receipt(
        "create_periodic_agent", data, source_group, deps
    )
    if receipt is cop_gate_module.ReceiptVerification.INVALID:
        return False
    if receipt is not cop_gate_module.ReceiptVerification.VALID:
        prompt_preview = request.prompt[:500]
        summary = (
            f"name={request.name}, profile={request.profile}, "
            f"schedule={request.schedule}, prompt={prompt_preview}"
        )
        allowed = await cop_gate_module.cop_gate(
            "create_periodic_agent",
            summary,
            data,
            source_group,
            deps,
        )
        if not allowed:
            return False
    return True


register("register_group", _handle_register_group)
register("create_periodic_agent", _handle_create_periodic_agent)
