"""Public cancellation contract for active Temporal scheduled workflows."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from temporalio.service import RPCError, RPCStatusCode

from pynchy.host.orchestrator.temporal.api import cancel_scheduled_agent_workflow
from pynchy.host.orchestrator.temporal.workflow_control import (
    bind_workflow_client,
    unbind_workflow_client,
)


@dataclass
class _Handle:
    cancel_error: RPCError | None = None
    cancelled: bool = False

    async def cancel(self) -> None:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled = True


@dataclass
class _Client:
    handles: dict[str, _Handle] = field(default_factory=dict)
    requested_workflow_ids: list[str] = field(default_factory=list)

    def get_workflow_handle(self, workflow_id: str, *, run_id: str | None = None) -> _Handle:
        assert run_id is None
        self.requested_workflow_ids.append(workflow_id)
        return self.handles[workflow_id]


@pytest.mark.asyncio
async def test_cancels_active_scheduled_workflow_by_durable_identity() -> None:
    handle = _Handle()
    client = _Client(handles={"scheduled-task-1": handle})
    bind_workflow_client(client)

    try:
        cancelled = await cancel_scheduled_agent_workflow("scheduled-task-1")
    finally:
        unbind_workflow_client(client)

    assert cancelled is True
    assert handle.cancelled is True
    assert client.requested_workflow_ids == ["scheduled-task-1"]


@pytest.mark.asyncio
async def test_cancellation_reports_disappeared_workflow_without_failing_retirement() -> None:
    handle = _Handle(RPCError("missing", RPCStatusCode.NOT_FOUND, b""))
    client = _Client(handles={"scheduled-task-removed": handle})
    bind_workflow_client(client)

    try:
        cancelled = await cancel_scheduled_agent_workflow("scheduled-task-removed")
    finally:
        unbind_workflow_client(client)

    assert cancelled is False
    assert handle.cancelled is False


@pytest.mark.asyncio
async def test_stopping_an_older_runtime_keeps_the_new_runtime_bound() -> None:
    old_client = _Client(handles={"scheduled-task-2": _Handle()})
    current_handle = _Handle()
    current_client = _Client(handles={"scheduled-task-2": current_handle})
    bind_workflow_client(old_client)
    bind_workflow_client(current_client)
    unbind_workflow_client(old_client)

    try:
        cancelled = await cancel_scheduled_agent_workflow("scheduled-task-2")
    finally:
        unbind_workflow_client(current_client)

    assert cancelled is True
    assert old_client.requested_workflow_ids == []
    assert current_handle.cancelled is True
