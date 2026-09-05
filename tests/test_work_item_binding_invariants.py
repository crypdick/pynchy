"""Post-write disappearance guards for durable work-item ownership."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pynchy.state import (
    bind_work_item_execution_to_task,
    bind_work_item_execution_to_turn,
    cancel_work_item_execution,
    create_work_item_claim,
)
from pynchy.work_items.api import WorkItemClaimRequest

pytest_plugins = ("tests.state_support",)


def _request() -> WorkItemClaimRequest:
    return WorkItemClaimRequest(
        workspace="project",
        issue={
            "id": "issue-1",
            "identifier": "SYN-1",
            "url": "https://linear.app/example/issue/1",
            "state": {"id": "todo", "name": "Todo"},
        },
        turn_id=None,
        task_id=None,
        initiated_by="test",
        request_id="claim-1",
    )


class _Cursor:
    def __init__(self, *, rowcount: int, row: object = None) -> None:
        self.rowcount = rowcount
        self._row = row

    async def fetchone(self) -> object:
        return self._row


class _Database:
    def __init__(self, *cursors: _Cursor) -> None:
        self._cursors = list(cursors)

    async def execute(self, *_args: object) -> _Cursor:
        return self._cursors.pop(0)


class _AtomicWrite:
    def __init__(self, database: _Database) -> None:
        self.database = database

    async def __aenter__(self) -> _Database:
        return self.database

    async def __aexit__(self, *_args: object) -> bool:
        return False


async def test_turn_binding_fails_if_the_execution_disappears_after_update() -> None:
    execution = await create_work_item_claim(_request())

    with (
        patch(
            "pynchy.state.work_item_bindings.get_work_item_execution",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(RuntimeError, match="disappeared while binding its turn"),
    ):
        await bind_work_item_execution_to_turn(
            execution.id,
            turn_id="turn-1",
            task_id=None,
        )


async def test_task_binding_fails_if_the_execution_disappears_after_update() -> None:
    execution = await create_work_item_claim(_request())

    with (
        patch(
            "pynchy.state.work_item_bindings.get_work_item_execution",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(RuntimeError, match="disappeared while binding its task"),
    ):
        await bind_work_item_execution_to_task(
            execution.id,
            task_id="task-1",
            temporal_workflow_id="workflow-1",
        )


async def test_cancellation_fails_if_the_execution_disappears_after_update() -> None:
    database = _Database(_Cursor(rowcount=1), _Cursor(rowcount=1))

    with (
        patch(
            "pynchy.state.work_item_cancellation.atomic_write",
            return_value=_AtomicWrite(database),
        ),
        pytest.raises(RuntimeError, match="disappeared during cancellation"),
    ):
        await cancel_work_item_execution("execution-1", blocker="reset")
