"""Automation IPC registration contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps

from pynchy.host.container_manager.ipc.bootstrap import register_builtin_handlers
from pynchy.host.container_manager.ipc.registry import HANDLERS, dispatch
from pynchy.host.container_manager.security.identity import ReceiptVerification


class _AutomationDeps(NullIpcDeps):
    def __init__(self) -> None:
        self.get_automation_status = AsyncMock(return_value=[{"name": "daily"}])
        self.get_automation_definition = AsyncMock(return_value={"name": "daily"})
        self.mutate_automation = AsyncMock()


def test_builtin_handlers_register_automation_operations() -> None:
    register_builtin_handlers()

    assert {
        "automation_status",
        "automation_definition",
        "create_automation",
        "update_automation",
        "pause_automation",
        "resume_automation",
        "delete_automation",
    } <= HANDLERS.keys()


@pytest.mark.asyncio
async def test_automation_read_handlers_project_results_and_ignore_incomplete_requests(
    tmp_path,
) -> None:
    register_builtin_handlers()
    deps = _AutomationDeps()
    response_path = tmp_path / "response.json"

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_automations.ipc_response_path",
            return_value=response_path,
        ),
        patch("pynchy.host.container_manager.ipc.handlers_automations.write_ipc_response") as write,
    ):
        await dispatch({"type": "automation_status", "request_id": "status"}, "admin", True, deps)
        await dispatch(
            {"type": "automation_definition", "request_id": "definition", "name": "daily"},
            "admin",
            True,
            deps,
        )
        deps.get_automation_definition.return_value = None
        await dispatch(
            {"type": "automation_definition", "request_id": "missing", "name": "absent"},
            "admin",
            True,
            deps,
        )
        await dispatch({"type": "automation_status"}, "admin", True, deps)
        await dispatch(
            {"type": "automation_definition", "request_id": "no-name"}, "admin", True, deps
        )

    assert write.call_args_list[-1].args[1] == {"error": "Automation not found"}
    assert deps.get_automation_status.await_count == 1
    assert deps.get_automation_definition.await_count == 2


@pytest.mark.asyncio
async def test_automation_handlers_enforce_dependency_and_approval_boundaries() -> None:
    register_builtin_handlers()
    with pytest.raises(TypeError, match="AutomationDeps"):
        await dispatch(
            {"type": "automation_status", "request_id": "wrong-deps"},
            "admin",
            True,
            NullIpcDeps(),
        )

    deps = _AutomationDeps()
    request = {
        "type": "create_automation",
        "request_id": "create",
        "name": "daily",
        "prompt": "run",
    }
    await dispatch(request, "admin", False, deps)

    with patch(
        "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
        new_callable=AsyncMock,
        return_value=ReceiptVerification.INVALID,
    ):
        await dispatch(request, "admin", True, deps)

    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
            new_callable=AsyncMock,
            return_value=ReceiptVerification.ABSENT,
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            side_effect=[False, True],
        ),
    ):
        await dispatch(request, "admin", True, deps)
        await dispatch(
            {**request, "type": "update_automation", "enabled": False}, "admin", True, deps
        )

    with patch(
        "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
        new_callable=AsyncMock,
        return_value=ReceiptVerification.VALID,
    ):
        await dispatch(request, "admin", True, deps)

    assert deps.mutate_automation.await_args_list[-2].args == (
        "update_automation",
        "daily",
        {"prompt": "run", "enabled": False},
    )
    assert deps.mutate_automation.await_args_list[-1].args == (
        "create_automation",
        "daily",
        {"prompt": "run"},
    )


@pytest.mark.asyncio
async def test_automation_mutation_supplies_request_id_for_human_approval() -> None:
    register_builtin_handlers()
    deps = _AutomationDeps()
    request = {
        "type": "create_automation",
        "request_id": "create",
        "name": "daily",
        "prompt": "run",
    }

    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
            new_callable=AsyncMock,
            return_value=ReceiptVerification.ABSENT,
        ),
        patch(
            "pynchy.host.container_manager.ipc.handlers_automations.cop_gate_module.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as cop_gate,
    ):
        await dispatch(request, "admin", True, deps)

    assert cop_gate.await_args.kwargs["request_id"] == "create"
    deps.mutate_automation.assert_not_awaited()
