"""Public validation and ownership boundaries for Linear work-item actions."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.api import attach_work_item_pull_request
from pynchy.plugins.integrations.linear_work_items import handle_list_work_items
from pynchy.work_items.api import WorkItemTransitionStatus
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


async def test_awaiting_review_attaches_github_pr_evidence(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-with-pr",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={
            "summary": "Implementation is ready for review.",
            "evidence_refs": [
                "https://github.com/crypdick/pynchy/pull/104",
                "ddfdace8",
            ],
        },
    )

    assert result["result"]["work_item"]["status"] == "awaiting_review"
    assert lifecycle.state.attachments == [
        {
            "id": "attachment-1",
            "url": "https://github.com/crypdick/pynchy/pull/104",
            "title": "crypdick/pynchy #104",
            "subtitle": None,
        }
    ]
    linked = await lifecycle.client.find_issues_by_attachment_url(
        "https://github.com/crypdick/pynchy/pull/104"
    )
    assert linked[0]["issue"]["id"] == "issue-1"


async def test_host_publication_attachment_supports_routing_lookup(
    lifecycle: Lifecycle,
) -> None:
    error = await attach_work_item_pull_request(
        "pynchy",
        "issue-1",
        "crypdick/pynchy",
        "https://github.com/crypdick/pynchy/pull/104",
    )

    assert error is None
    linked = await lifecycle.client.find_issues_by_attachment_url(
        "https://github.com/crypdick/pynchy/pull/104"
    )
    assert linked[0]["title"] == "crypdick/pynchy #104"
    assert linked[0]["issue"]["id"] == "issue-1"


async def test_awaiting_review_backfills_preserved_pr_evidence(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    blocked = await _call(
        lifecycle,
        "linear_move_todo",
        "move-blocked-with-pr",
        issue_id="issue-1",
        status="blocked",
        outcome={
            "blocker": "Waiting for provider review.",
            "evidence_refs": ["https://github.com/crypdick/pynchy/pull/104"],
        },
    )
    assert blocked["result"]["work_item"]["status"] == "blocked"
    assert lifecycle.state.attachments == []

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-ready-with-preserved-pr",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Provider review arrived."},
    )

    assert result["result"]["work_item"]["status"] == "awaiting_review"
    assert result["result"]["work_item"]["evidence_refs"] == [
        "https://github.com/crypdick/pynchy/pull/104"
    ]
    assert lifecycle.state.attachments[0]["url"] == ("https://github.com/crypdick/pynchy/pull/104")


async def test_awaiting_review_stays_in_progress_when_pr_attachment_fails(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    lifecycle.state.attachment_success = False

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-with-failed-pr",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={
            "summary": "Implementation is ready for review.",
            "evidence_refs": ["https://github.com/crypdick/pynchy/pull/104"],
        },
    )

    assert result == {"error": "Linear did not create the attachment"}
    assert lifecycle.state.issue["state"]["name"] == "In Progress"


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


async def test_work_item_listing_requires_configured_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pynchy.plugins.integrations.linear_work_items._runtime.runtime", None)

    with pytest.raises(RuntimeError, match="Linear work-items runtime has not been configured"):
        await handle_list_work_items({"source_group": "pynchy"})


async def test_linked_move_converts_provider_errors_to_action_errors(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _lease(lifecycle)
    await _begin_turn()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_items.transition_linked_work_item",
        AsyncMock(side_effect=ValueError("provider rejected move")),
    )

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Implementation is ready."},
    )

    assert result == {"error": "provider rejected move"}


async def test_reconcile_converts_provider_errors_to_action_errors(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _lease(lifecycle)
    await _begin_turn()
    lifecycle.state.fail_after_update = True
    uncertain = await _call(
        lifecycle,
        "linear_move_todo",
        "move-uncertain",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "The provider response was lost."},
    )
    assert uncertain["result"]["work_item"]["status"] == WorkItemTransitionStatus.UNKNOWN.value
    lifecycle.state.fail_after_update = False
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_items.reconcile_work_item",
        AsyncMock(side_effect=ValueError("reconciliation failed")),
    )

    result = await _call(
        lifecycle,
        "linear_reconcile_work_item",
        "reconcile-uncertain",
        issue_id="issue-1",
    )

    assert result == {"error": "reconciliation failed"}


async def test_work_item_actions_require_source_group(lifecycle: Lifecycle) -> None:
    with pytest.raises(ValueError, match="source_group is required"):
        await lifecycle.handlers["linear_move_todo"](
            {
                "request_id": "move-1",
                "issue_id": "issue-1",
                "status": "done",
            }
        )
