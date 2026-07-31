"""Hermetic tests for Linear authority, leasing, and generic agent actions."""

from __future__ import annotations

import pytest

from pynchy.state import (
    get_work_item_transition_by_request,
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


async def test_repeated_outcome_updates_metadata_without_provider_write(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Checks passed.", "evidence_refs": ["tests:focused"]},
    )
    update_calls = lifecycle.state.update_calls

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-2",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Checks and publication passed.", "evidence_refs": ["pr:10"]},
    )

    work_item = result["result"]["work_item"]
    assert lifecycle.state.update_calls == update_calls
    assert work_item["status"] == "awaiting_review"
    assert work_item["summary"] == "Checks and publication passed."
    assert work_item["evidence_refs"] == ["pr:10"]


@pytest.mark.action("linear.workitem.reconcile")
async def test_reconcile_repairs_reviewed_conflict_at_target_state(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    lifecycle.state.issue["state"] = _state("state-human-approved")
    conflicted = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Ready for review."},
    )
    lifecycle.state.issue["state"] = _state("state-awaiting-review")
    update_calls = lifecycle.state.update_calls

    repaired = await _call(
        lifecycle,
        "linear_reconcile_work_item",
        "reconcile-1",
        issue_id="issue-1",
    )

    transition = await get_work_item_transition_by_request("move-1")
    assert conflicted["result"]["work_item"]["status"] == "failed"
    assert repaired["result"]["work_item"]["status"] == "awaiting_review"
    assert lifecycle.state.update_calls == update_calls
    assert transition is not None
    assert transition.status.value == "succeeded"
    assert transition.receipt is not None
    assert transition.error is None


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
