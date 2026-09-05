"""IPC handlers for config-backed automation management."""

from __future__ import annotations

from typing import Any

from pynchy.host.container_manager.ipc.deps import (
    AutomationDeps,
    IpcDeps,
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security import cop_gate as cop_gate_module
from pynchy.logger import logger

_MUTATIONS = frozenset(
    {
        "create_automation",
        "update_automation",
        "pause_automation",
        "resume_automation",
        "delete_automation",
    }
)


def _request_id(data: dict[str, Any]) -> str | None:
    request_id = data.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else None


def _name(data: dict[str, Any]) -> str | None:
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def _automation_deps(deps: IpcDeps) -> AutomationDeps:
    if not isinstance(deps, AutomationDeps):
        raise TypeError("Automation handlers require AutomationDeps")
    return deps


async def _handle_automation_status(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - IPC handler contract.
    deps: IpcDeps,
) -> None:
    request_id = _request_id(data)
    if request_id is None:
        return
    automations = await _automation_deps(deps).get_automation_status(
        source_group=source_group,
        is_admin=is_admin,
    )
    write_ipc_response(
        ipc_response_path(source_group, request_id),
        {"result": {"automations": automations}},
    )


async def _handle_automation_definition(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - IPC handler contract.
    deps: IpcDeps,
) -> None:
    request_id = _request_id(data)
    name = _name(data)
    if request_id is None or name is None:
        return
    definition = await _automation_deps(deps).get_automation_definition(
        name,
        source_group=source_group,
        is_admin=is_admin,
    )
    response: dict[str, object] = {"error": "Automation not found"}
    if definition is not None:
        response = {"result": definition}
    write_ipc_response(ipc_response_path(source_group, request_id), response)


async def _handle_automation_mutation(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - IPC handler contract.
    deps: IpcDeps,
) -> None:
    operation = data.get("type")
    name = _name(data)
    if operation not in _MUTATIONS or name is None or not is_admin:
        logger.warning("Unauthorized or invalid automation request", operation=operation)
        return
    receipt = await cop_gate_module.verify_approval_receipt(operation, data, source_group, deps)
    if receipt is cop_gate_module.ReceiptVerification.INVALID:
        return
    if receipt is not cop_gate_module.ReceiptVerification.VALID:
        summary = f"automation={name}, operation={operation}"
        if not await cop_gate_module.cop_gate(
            operation, summary, data, source_group, deps, request_id=_request_id(data)
        ):
            return
    envelope_fields = {"type", "request_id", "name", "source_group", "reply_to", "deadline"}
    values = {key: value for key, value in data.items() if key not in envelope_fields}
    await _automation_deps(deps).mutate_automation(operation, name, values)


register("automation_status", _handle_automation_status)
register("automation_definition", _handle_automation_definition)
for _operation in _MUTATIONS:
    register(_operation, _handle_automation_mutation)
