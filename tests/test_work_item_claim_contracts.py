"""Public work-item claim conflict and persistence invariants."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aiosqlite import IntegrityError

from pynchy.state import (
    create_work_item_claim,
    get_work_item_execution_for_turn,
)
from pynchy.work_items.api import (
    WorkItemClaimConflictError,
    WorkItemClaimRequest,
)

pytest_plugins = ("tests.state_support",)


def _request(
    issue_id: str = "issue-1",
    *,
    request_id: str | None = None,
    turn_id: str | None = None,
) -> WorkItemClaimRequest:
    return WorkItemClaimRequest(
        workspace="pynchy",
        issue={
            "id": issue_id,
            "identifier": f"SYN-{issue_id}",
            "url": f"https://linear.app/example/issue/{issue_id}",
            "state": {"id": "todo", "name": "Todo"},
        },
        turn_id=turn_id,
        task_id=None,
        initiated_by="test",
        request_id=request_id or f"claim-{issue_id}",
    )


async def test_duplicate_claim_reports_the_active_execution() -> None:
    first = await create_work_item_claim(_request())

    with pytest.raises(WorkItemClaimConflictError) as raised:
        await create_work_item_claim(_request())

    assert raised.value.execution.id == first.id


async def test_unrelated_claim_integrity_error_is_not_reclassified() -> None:
    await create_work_item_claim(_request())

    with pytest.raises(IntegrityError):
        await create_work_item_claim(_request("issue-2", request_id="claim-issue-1"))


class _EmptyCursor:
    async def fetchone(self) -> None:
        return None


class _EmptyDatabase:
    async def execute(self, *_args: object) -> _EmptyCursor:
        return _EmptyCursor()


class _AtomicWrite:
    async def __aenter__(self) -> _EmptyDatabase:
        return _EmptyDatabase()

    async def __aexit__(self, *_args: object) -> bool:
        return False


async def test_claim_rejects_missing_attempt_count_row() -> None:
    with (
        patch("pynchy.state.work_items.atomic_write", return_value=_AtomicWrite()),
        pytest.raises(RuntimeError, match="attempt count query returned no row"),
    ):
        await create_work_item_claim(_request())


async def test_execution_lookup_by_turn_returns_bound_execution() -> None:
    execution = await create_work_item_claim(_request(turn_id="turn-1"))

    assert await get_work_item_execution_for_turn("turn-1") == execution
    assert await get_work_item_execution_for_turn("turn-missing") is None
