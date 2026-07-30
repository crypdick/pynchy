"""Regression coverage for interrupted and out-of-order Linear transitions."""

from __future__ import annotations

from typing import Any

from pynchy.state import get_work_item_transition_by_request
from pynchy.work_items.api import WorkItemExecutionStatus, WorkItemTransitionStatus
from tests.linear_work_items_support import (
    Lifecycle,
    _begin_turn,
    _call,
    _lease,
    _state,
)

pytest_plugins = ("tests.linear_work_items_support",)


async def test_lease_retry_reapplies_an_uncertain_write_that_did_not_land(
    lifecycle: Lifecycle,
) -> None:
    query = lifecycle.client.query
    first = True

    async def fail_before_update(
        statement: str,
        **variables: object,
    ) -> dict[str, Any]:
        nonlocal first
        if first:
            first = False
            raise RuntimeError("connection closed before provider accepted mutation")
        return await query(statement, **variables)

    lifecycle.client.query = fail_before_update  # type: ignore[method-assign]

    uncertain = await _lease(lifecycle)
    resumed = await _lease(lifecycle)

    assert uncertain.status is WorkItemExecutionStatus.UNKNOWN
    assert resumed.status is WorkItemExecutionStatus.IN_PROGRESS
    assert resumed.id == uncertain.id
    assert lifecycle.state.issue["state"]["name"] == "In Progress"


async def test_stale_transition_receipt_cannot_overwrite_newer_lifecycle(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    lifecycle.state.fail_after_update = True
    uncertain = await _call(
        lifecycle,
        "linear_move_todo",
        "move-reviewed",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Review move outcome was uncertain."},
    )
    lifecycle.state.fail_after_update = False
    blocked = await _call(
        lifecycle,
        "linear_move_todo",
        "move-blocked",
        issue_id="issue-1",
        status="blocked",
        outcome={"blocker": "A newer lifecycle decision superseded review."},
    )
    lifecycle.state.issue["state"] = _state("state-awaiting-review")

    stale = await _call(
        lifecycle,
        "linear_reconcile_work_item",
        "reconcile-reviewed",
        issue_id="issue-1",
    )

    assert uncertain["result"]["work_item"]["status"] == "unknown"
    assert blocked["result"]["work_item"]["status"] == "blocked"
    assert stale["result"]["work_item"]["status"] == "blocked"
    transition = await get_work_item_transition_by_request("move-reviewed")
    assert transition is not None
    assert transition.status is WorkItemTransitionStatus.SUCCEEDED
