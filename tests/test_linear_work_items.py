"""Hermetic tests for Linear authority, leasing, and generic agent actions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_work_item_completion import (
    complete_reviewed_work_item,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    WorkItemLeaseRequest,
    acquire_human_started_work_item_lease,
)
from pynchy.state import (
    cancel_work_item_execution,
    clear_in_flight_turn,
    get_active_work_item_execution,
    get_unfinished_work_item_execution,
    get_work_item_execution_for_issue,
    get_work_item_transition_by_request,
    mark_work_item_delivery_delivered_for_turn,
)
from pynchy.work_items.api import (
    WorkItemClaimConflictError,
    WorkItemExecutionStatus,
)
from tests.linear_work_items_support import (
    Lifecycle,
    _begin_turn,
    _board,
    _call,
    _lease,
    _state,
)

pytest_plugins = ("tests.linear_work_items_support",)


@pytest.mark.action("linear.todo.plan")
async def test_submit_plan_persists_markdown_and_waits_for_human_approval(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.issue["state"] = _state("state-ready-for-planning")
    lifecycle.state.issue["description"] = "Acceptance criteria from the user."

    result = await _call(
        lifecycle,
        "linear_submit_plan",
        "plan-1",
        issue_id="issue-1",
        plan="1. Add the failing test.\n2. Implement the behavior.\n3. Run the full gate.",
    )

    issue = result["result"]["issue"]
    assert issue["state"]["name"] == "Awaiting Plan Approval"
    assert lifecycle.state.issue["description"].startswith("Acceptance criteria from the user.")
    assert "<!-- pynchy.plan:start -->" in lifecycle.state.issue["description"]
    assert "1. Add the failing test." in lifecycle.state.issue["description"]
    assert "<!-- pynchy.plan:end -->" in lifecycle.state.issue["description"]
    assert await get_active_work_item_execution("issue-1") is None


async def test_submit_plan_replaces_unapproved_plan_without_lifecycle_churn(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.issue["state"] = _state("state-awaiting-plan-approval")
    lifecycle.state.issue["description"] = (
        "Acceptance criteria from the user.\n\n"
        "<!-- pynchy.plan:start -->\n"
        "## Pynchy implementation plan\n\n"
        "1. Follow the stale assumptions.\n"
        "<!-- pynchy.plan:end -->"
    )

    result = await _call(
        lifecycle,
        "linear_submit_plan",
        "plan-revision-1",
        issue_id="issue-1",
        plan="1. Re-read current behavior.\n2. Replace the stale assumptions.",
    )

    issue = result["result"]["issue"]
    assert issue["state"]["name"] == "Awaiting Plan Approval"
    assert lifecycle.state.issue["description"].startswith("Acceptance criteria from the user.")
    assert lifecycle.state.issue["description"].count("<!-- pynchy.plan:start -->") == 1
    assert "2. Replace the stale assumptions." in lifecycle.state.issue["description"]
    assert "Follow the stale assumptions." not in lifecycle.state.issue["description"]
    assert await get_active_work_item_execution("issue-1") is None


async def test_submit_plan_requires_planning_or_approval_wait_state(
    lifecycle: Lifecycle,
) -> None:
    result = await _call(
        lifecycle,
        "linear_submit_plan",
        "plan-1",
        issue_id="issue-1",
        plan="A concrete plan.",
    )

    assert result == {
        "error": (
            "Linear work item must be Ready for Planning or Awaiting Plan Approval before planning"
        )
    }


async def test_host_lease_persists_before_moving_to_in_progress(
    lifecycle: Lifecycle,
) -> None:
    execution = await _lease(lifecycle)

    assert execution.status.value == "in_progress"
    assert execution.attempt == 1
    assert lifecycle.state.issue["state"]["name"] == "In Progress"
    assert await get_active_work_item_execution("issue-1") is not None


async def test_host_lease_requires_human_approved(lifecycle: Lifecycle) -> None:
    lifecycle.state.issue["state"] = _state("state-agent-proposed")

    with pytest.raises(ValueError, match="must be Human Approved"):
        await _lease(lifecycle)

    assert await get_active_work_item_execution("issue-1") is None


async def test_human_started_lease_adopts_in_progress_without_provider_move(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.issue["state"] = _state("state-in-progress")

    execution = await acquire_human_started_work_item_lease(
        lifecycle.client,
        WorkItemLeaseRequest(
            workspace="pynchy",
            issue_id="issue-1",
            request_id="human-started-1",
            initiated_by="linear-webhook:delivery-1:user:user-1",
        ),
    )

    assert execution.status.value == "in_progress"
    assert lifecycle.state.issue["state"]["name"] == "In Progress"
    assert await get_active_work_item_execution("issue-1") is not None


async def test_human_started_lease_requires_current_in_progress_state(
    lifecycle: Lifecycle,
) -> None:
    with pytest.raises(ValueError, match="must be human-started"):
        await acquire_human_started_work_item_lease(
            lifecycle.client,
            WorkItemLeaseRequest(
                workspace="pynchy",
                issue_id="issue-1",
                request_id="human-started-1",
                initiated_by="linear-webhook:delivery-1:user:user-1",
            ),
        )

    assert await get_active_work_item_execution("issue-1") is None


async def test_lease_retry_reconciles_a_lost_provider_response(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.fail_after_update = True
    uncertain = await _lease(lifecycle)
    lifecycle.state.fail_after_update = False

    resumed = await _lease(lifecycle)

    assert uncertain.status.value == "unknown"
    assert resumed.status.value == "in_progress"
    assert resumed.id == uncertain.id


async def test_second_trigger_cannot_take_an_active_lease(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)

    with pytest.raises(WorkItemClaimConflictError):
        await _lease(lifecycle, "lease-2")


@pytest.mark.action("linear.todo.move")
async def test_agent_can_move_owned_work_to_awaiting_review(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Implementation is ready for review."},
    )

    work_item = result["result"]["work_item"]
    assert work_item["status"] == "awaiting_review"
    assert work_item["turn_id"] == "turn-1"
    assert await get_active_work_item_execution("issue-1") is None


async def test_successful_nonblocked_moves_clear_blocked_projection(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    await _call(
        lifecycle,
        "linear_move_todo",
        "move-blocked",
        issue_id="issue-1",
        status="blocked",
        outcome={
            "blocker": "Publication requires a write-capable credential.",
            "handoff_to": "release operator",
        },
    )

    reviewed = await _call(
        lifecycle,
        "linear_move_todo",
        "move-reviewed",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Publication succeeded."},
    )
    completed = await _call(
        lifecycle,
        "linear_move_todo",
        "move-completed",
        issue_id="issue-1",
        status="done",
        outcome={"summary": "The reviewed change is merged and deployed."},
    )

    reviewed_item = reviewed["result"]["work_item"]
    assert reviewed_item["status"] == "awaiting_review"
    assert reviewed_item["blocker"] is None
    assert reviewed_item["handoff_to"] is None
    completed_item = completed["result"]["work_item"]
    assert completed_item["status"] == "completed"
    assert completed_item["blocker"] is None
    assert completed_item["handoff_to"] is None

    blocked_transition = await get_work_item_transition_by_request("move-blocked")
    assert blocked_transition is not None
    assert blocked_transition.blocker == "Publication requires a write-capable credential."
    assert blocked_transition.handoff_to == "release operator"


async def test_unknown_nonblocked_move_preserves_blocker_until_reconciled(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    await _call(
        lifecycle,
        "linear_move_todo",
        "move-blocked",
        issue_id="issue-1",
        status="blocked",
        outcome={
            "blocker": "Publication requires a write-capable credential.",
            "handoff_to": "release operator",
        },
    )
    lifecycle.state.fail_after_update = True

    uncertain = await _call(
        lifecycle,
        "linear_move_todo",
        "move-reviewed",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Publication may have succeeded."},
    )

    uncertain_item = uncertain["result"]["work_item"]
    assert uncertain_item["status"] == "unknown"
    assert uncertain_item["blocker"] == "Publication requires a write-capable credential."
    assert uncertain_item["handoff_to"] == "release operator"

    lifecycle.state.fail_after_update = False
    reconciled = await _call(
        lifecycle,
        "linear_reconcile_work_item",
        "reconcile-reviewed",
        issue_id="issue-1",
    )

    reconciled_item = reconciled["result"]["work_item"]
    assert reconciled_item["status"] == "awaiting_review"
    assert reconciled_item["blocker"] is None
    assert reconciled_item["handoff_to"] is None


async def test_conflicting_nonblocked_move_preserves_prior_blocker(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    await _call(
        lifecycle,
        "linear_move_todo",
        "move-blocked",
        issue_id="issue-1",
        status="blocked",
        outcome={
            "blocker": "Publication requires a write-capable credential.",
            "handoff_to": "release operator",
        },
    )
    lifecycle.state.fail_after_update = True
    await _call(
        lifecycle,
        "linear_move_todo",
        "move-reviewed",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={"summary": "Publication may have succeeded."},
    )
    lifecycle.state.fail_after_update = False
    lifecycle.state.issue["state"] = _state("state-blocked")

    conflicted = await _call(
        lifecycle,
        "linear_reconcile_work_item",
        "reconcile-reviewed",
        issue_id="issue-1",
    )

    conflicted_item = conflicted["result"]["work_item"]
    assert conflicted_item["status"] == "failed"
    assert conflicted_item["blocker"] == "Publication requires a write-capable credential."
    assert conflicted_item["handoff_to"] == "release operator"


async def test_blocked_work_can_be_reauthorized_for_a_new_attempt(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    blocked = await _call(
        lifecycle,
        "linear_move_todo",
        "move-blocked",
        issue_id="issue-1",
        status="blocked",
        outcome={"blocker": "Publication requires a write-capable credential."},
    )
    await clear_in_flight_turn("turn-1")
    await _begin_turn()
    approved = await _call(
        lifecycle,
        "linear_move_todo",
        "move-approved",
        issue_id="issue-1",
        status="human_approved",
    )
    retried = await _lease(lifecycle, "lease-2")

    assert blocked["result"]["work_item"]["status"] == "blocked"
    assert approved["result"]["issue"]["state"]["name"] == "Human Approved"
    assert retried.attempt == 2
    await cancel_work_item_execution(retried.id, blocker="test completed")
    assert await get_unfinished_work_item_execution("issue-1") is None


async def test_blocked_linked_work_requires_durable_blocker_evidence(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-blocked",
        issue_id="issue-1",
        status="blocked",
    )

    assert result == {"error": "outcome.blocker is required when moving work to Blocked"}
    execution = await get_work_item_execution_for_issue("issue-1", workspace="pynchy")
    assert execution is not None
    assert execution.status.value == "in_progress"
    assert execution.blocker is None
    assert lifecycle.state.issue["state"]["name"] == "In Progress"


async def test_later_blocked_outcome_rebinds_only_requester_delivery(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(turn_id="turn-owner", input_source="scheduled_task")
    await _call(
        lifecycle,
        "linear_move_todo",
        "move-awaiting",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={
            "summary": "Implementation is ready.",
            "evidence_refs": ["tests:focused", "deploy:staging"],
        },
    )
    await clear_in_flight_turn("turn-owner")
    await _begin_turn(turn_id="turn-follow-ups", input_source="trusted:linear:follow-ups")
    await _call(
        lifecycle,
        "linear_move_todo",
        "move-follow-ups",
        issue_id="issue-1",
        status="follow_ups",
        outcome={"summary": "One deployment follow-up remains."},
    )
    await clear_in_flight_turn("turn-follow-ups")
    await _begin_turn(turn_id="turn-blocked", input_source="trusted:linear:follow-ups")

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-blocked",
        issue_id="issue-1",
        status="blocked",
        outcome={
            "blocker": "The deployment credential is unavailable.",
            "handoff_to": "release operator",
        },
    )

    work_item = result["result"]["work_item"]
    assert work_item["turn_id"] == "turn-owner"
    assert work_item["summary"] == "The deployment credential is unavailable."
    assert work_item["blocker"] == "The deployment credential is unavailable."
    assert work_item["handoff_to"] == "release operator"
    assert work_item["evidence_refs"] == ["tests:focused", "deploy:staging"]
    assert work_item["requester_delivery"] == {
        "status": "pending",
        "turn_id": "turn-blocked",
        "error": None,
        "delivered_at": None,
    }

    await mark_work_item_delivery_delivered_for_turn("turn-owner")
    await mark_work_item_delivery_delivered_for_turn("turn-follow-ups")
    still_pending = await get_work_item_execution_for_issue("issue-1", workspace="pynchy")
    assert still_pending is not None
    assert still_pending.requester_delivery_status == "pending"
    assert still_pending.requester_delivery_turn_id == "turn-blocked"

    await mark_work_item_delivery_delivered_for_turn("turn-blocked")
    delivered = await get_work_item_execution_for_issue("issue-1", workspace="pynchy")
    assert delivered is not None
    assert delivered.turn_id == "turn-owner"
    assert delivered.requester_delivery_status == "delivered"
    assert delivered.requester_delivery_turn_id == "turn-blocked"
    assert delivered.requester_delivered_at is not None


@pytest.mark.parametrize("status", ["human_approved", "rejected"])
async def test_agent_cannot_assert_human_decisions(
    lifecycle: Lifecycle,
    status: str,
) -> None:
    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status=status,
    )

    assert "direct-human instruction" in result["error"]


@pytest.mark.parametrize("status", ["human_approved", "rejected"])
async def test_direct_human_can_move_unlinked_work(
    lifecycle: Lifecycle,
    status: str,
) -> None:
    lifecycle.state.issue["state"] = _state("state-agent-proposed")
    await _begin_turn()

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status=status,
    )

    assert result["result"]["issue"]["state"]["name"] == _board().states[status]["name"]


async def test_mark_done_needs_no_authorization_quote(lifecycle: Lifecycle) -> None:
    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="done",
    )

    assert result["result"]["issue"]["state"]["name"] == "Done"


async def test_agent_can_finish_follow_ups_in_a_later_turn(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    awaiting = await _call(
        lifecycle,
        "linear_move_todo",
        "move-awaiting",
        issue_id="issue-1",
        status="awaiting_review",
        outcome={
            "summary": "Implementation and focused tests are complete.",
            "evidence_refs": ["tests:focused"],
        },
    )
    await clear_in_flight_turn("turn-1")
    await _begin_turn(turn_id="turn-2", input_source="trusted:linear:follow-ups")

    follow_ups = await _call(
        lifecycle,
        "linear_move_todo",
        "move-follow-ups",
        issue_id="issue-1",
        status="follow_ups",
        outcome={"summary": "Verifying the deployed result."},
    )
    done = await _call(
        lifecycle,
        "linear_move_todo",
        "move-done",
        issue_id="issue-1",
        status="done",
        outcome={"summary": "Deployment verified and cleanup complete."},
    )

    assert awaiting["result"]["work_item"]["turn_id"] == "turn-1"
    assert follow_ups["result"]["work_item"]["status"] == "follow_ups"
    assert follow_ups["result"]["work_item"]["turn_id"] == "turn-1"
    assert follow_ups["result"]["work_item"]["requester_delivery"]["turn_id"] == "turn-2"
    assert follow_ups["result"]["work_item"]["evidence_refs"] == ["tests:focused"]
    assert done["result"]["work_item"]["status"] == "completed"


async def test_only_direct_human_can_reopen_terminal_work(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.issue["state"] = _state("state-done")
    denied = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="agent_proposed",
    )
    await _begin_turn()
    reopened = await _call(
        lifecycle,
        "linear_move_todo",
        "move-2",
        issue_id="issue-1",
        status="agent_proposed",
    )

    assert "direct-human instruction" in denied["error"]
    assert reopened["result"]["issue"]["state"]["name"] == "Agent Proposed"


async def test_in_progress_is_always_host_managed(lifecycle: Lifecycle) -> None:
    await _begin_turn()

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="in_progress",
    )

    assert result == {"error": "In Progress is managed by the host execution lease"}


async def test_active_execution_rejects_nonterminal_agent_move(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="agent_proposed",
    )

    assert "active execution must move" in result["error"]


async def test_direct_human_done_completes_active_execution(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn()

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="done",
        outcome={"summary": "The requested work is complete."},
    )

    assert result["result"]["work_item"]["status"] == "completed"
    assert await get_active_work_item_execution("issue-1") is None


async def test_done_webhook_completes_execution_still_in_progress(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    lifecycle.state.issue["state"] = _state("state-done")

    completed = await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1")

    assert completed is not None
    assert completed.status.value == "completed"


async def test_done_webhook_without_execution_is_ignored(lifecycle: Lifecycle) -> None:
    assert await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1") is None


async def test_done_webhook_requires_configured_completion_runtime() -> None:
    with (
        patch("pynchy.plugins.integrations.linear_work_item_completion._runtime", None),
        pytest.raises(RuntimeError, match="runtime has not been configured"),
    ):
        await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1")


async def test_done_webhook_ignores_terminal_execution(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)
    await _begin_turn()
    await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="done",
        outcome={"summary": "The requested work is complete."},
    )

    assert await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1") is None


async def test_done_webhook_returns_none_when_provider_reconciliation_is_lost(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = await _lease(lifecycle)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_completion.reconcile_work_item",
        AsyncMock(return_value=None),
    )

    assert await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1") is None
    assert execution.status is WorkItemExecutionStatus.IN_PROGRESS


async def test_done_webhook_rejects_noncompleted_provider_reconciliation(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = await _lease(lifecycle)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_completion.reconcile_work_item",
        AsyncMock(return_value=execution),
    )

    with pytest.raises(LinearError, match="could not be reconciled"):
        await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1")
