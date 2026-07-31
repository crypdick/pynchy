"""Public validation and ownership boundaries for Linear work-item actions."""

from __future__ import annotations

import pytest

from tests.linear_work_items_support import (
    Lifecycle,
    _begin_turn,
    _call,
    _lease,
)

pytest_plugins = ("tests.linear_work_items_support",)


async def test_active_execution_owned_by_another_workspace_is_not_movable(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle, workspace="other")

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="done",
        outcome={"summary": "The requested work is complete."},
    )

    assert result == {"error": "No Pynchy execution owns this Linear work item"}


async def test_active_execution_rejects_human_status_move_even_from_a_direct_turn(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn()

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="human_approved",
    )

    assert "active execution must move" in result["error"]


async def test_claimed_work_cannot_reenter_planning(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)

    result = await _call(
        lifecycle,
        "linear_submit_plan",
        "plan-1",
        issue_id="issue-1",
        plan="A replacement plan.",
    )

    assert result["error"] == "A claimed Linear work item cannot re-enter planning"


async def test_linked_move_requires_the_current_agent_turn(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Implementation is ready."},
    )

    assert result == {"error": "A current agent turn is required to report a linked outcome"}


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("not-a-status", "status must be one of"),
        ("awaiting_review", "outcome.summary is required"),
    ],
)
async def test_move_rejects_invalid_status_or_missing_outcome(
    lifecycle: Lifecycle,
    status: str,
    error: str,
) -> None:
    await _lease(lifecycle)
    await _begin_turn()

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status=status,
    )

    assert error in result["error"]


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ([], "outcome must be an object"),
        ({"unexpected": "value"}, "unsupported fields"),
        ({"summary": 42}, "outcome.summary must be text"),
        ({"summary": "  "}, "outcome.summary must not be empty"),
        ({"summary": "ready", "blocker": "wrong status"}, "only valid for Blocked"),
        ({"summary": "ready", "evidence_refs": "not-an-array"}, "must be an array"),
        ({"summary": "ready", "evidence_refs": [""]}, "non-empty strings"),
    ],
)
async def test_linked_move_rejects_malformed_outcome_evidence(
    lifecycle: Lifecycle,
    outcome: object,
    error: str,
) -> None:
    await _lease(lifecycle)
    await _begin_turn()

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
        outcome=outcome,
    )

    assert error in result["error"]


async def test_provider_move_rejects_an_issue_with_malformed_state(lifecycle: Lifecycle) -> None:
    lifecycle.state.issue["state"] = None

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="done",
    )

    assert result == {"error": "Linear issue state was not an object"}


async def test_reconcile_requires_an_execution_and_unresolved_transition(
    lifecycle: Lifecycle,
) -> None:
    missing = await _call(
        lifecycle,
        "linear_reconcile_work_item",
        "reconcile-missing",
        issue_id="issue-1",
    )
    assert missing == {"error": "No Pynchy execution owns this Linear work item"}

    await _lease(lifecycle)
    unresolved = await _call(
        lifecycle,
        "linear_reconcile_work_item",
        "reconcile-unresolved",
        issue_id="issue-1",
    )
    assert unresolved == {"error": "No uncertain work-item transition needs reconciliation"}
