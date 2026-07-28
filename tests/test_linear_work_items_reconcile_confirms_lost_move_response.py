"""Hermetic tests for Linear authority, leasing, and generic agent actions."""

from __future__ import annotations

import pytest

from pynchy.state import (
    list_work_item_executions,
)
from tests.linear_work_items_support import (
    Lifecycle,
    _begin_turn,
    _call,
    _lease,
    _state,
)

pytest_plugins = ("tests.linear_work_items_support",)


@pytest.mark.action("linear.workitem.reconcile")
async def test_reconcile_confirms_lost_move_response(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    lifecycle.state.fail_after_update = True
    uncertain = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Ready despite a lost provider response."},
    )
    lifecycle.state.fail_after_update = False

    result = await _call(
        lifecycle,
        "linear_reconcile_work_item",
        "reconcile-1",
        issue_id="issue-1",
    )

    assert uncertain["result"]["work_item"]["status"] == "unknown"
    assert result["result"]["work_item"]["status"] == "awaiting_review"


async def test_remote_state_conflict_is_durable(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    lifecycle.state.issue["state"] = _state("state-human-approved")

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Ready for review."},
    )

    assert "conflicted" in result["error"]
    assert result["result"]["work_item"]["status"] == "failed"


@pytest.mark.action("linear.workitem.list")
async def test_list_returns_workspace_execution_projection(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)

    result = await _call(lifecycle, "linear_list_work_items", "list-1")

    assert result["result"]["work_items"][0]["issue"]["identifier"] == "PYN-1"
    assert len(await list_work_item_executions(workspace="pynchy")) == 1


async def test_lease_rejects_issue_from_another_workspace_board(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.issue["project"] = {"id": "project-other", "name": "Other"}

    with pytest.raises(ValueError, match="does not belong"):
        await _lease(lifecycle)
