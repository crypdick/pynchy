"""Failure-boundary tests for durable Linear work-item state operations."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pynchy.conversation.api import (
    ConversationClaimId,
    ConversationId,
    ConversationLifecycleFence,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.plugins.integrations.linear_work_item_completion import (
    complete_reviewed_work_item,
)
from pynchy.state import (
    begin_work_item_transition,
    begin_work_item_transition_if_lifecycle_current,
    cancel_work_item_execution,
    get_work_item_transition_by_request,
    resolve_work_item_transition,
)
from pynchy.work_items.api import (
    WorkItemExecutionStatus,
    WorkItemTransitionRequest,
    WorkItemTransitionStatus,
)
from tests.linear_work_items_support import Lifecycle, _lease

pytest_plugins = ("tests.linear_work_items_support",)


@pytest.mark.parametrize(
    ("issue", "error", "message"),
    [
        ({"url": "https://linear.app/example/issue/PYN-1", "state": {}}, ValueError, "identifier"),
        (
            {"identifier": "PYN-1", "url": "https://linear.app/example/issue/PYN-1"},
            TypeError,
            "state",
        ),
    ],
)
async def test_transition_resolution_rejects_incomplete_provider_issue(
    lifecycle: Lifecycle,
    issue: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    execution = await _lease(lifecycle)
    transition = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="invalid-issue",
            operation="request_review",
            target_status="awaiting_review",
            result_execution_status=WorkItemExecutionStatus.AWAITING_REVIEW,
        )
    )

    with pytest.raises(error, match=message):
        await resolve_work_item_transition(
            transition=transition,
            execution_status=WorkItemExecutionStatus.IN_PROGRESS,
            transition_status=WorkItemTransitionStatus.SUCCEEDED,
            issue=issue,
        )
    persisted = await get_work_item_transition_by_request("invalid-issue")
    assert persisted is not None
    assert persisted.status is WorkItemTransitionStatus.PENDING


async def test_transition_persistence_fails_closed_when_the_record_disappears(
    lifecycle: Lifecycle,
) -> None:
    execution = await _lease(lifecycle)
    request = WorkItemTransitionRequest(
        execution=execution,
        request_id="missing-transition",
        operation="record_handoff",
        target_status="blocked",
        result_execution_status=WorkItemExecutionStatus.BLOCKED,
    )

    with (
        patch(
            "pynchy.state.work_item_transitions.get_work_item_transition_by_request",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(RuntimeError, match="was not persisted"),
    ):
        await begin_work_item_transition(request)


async def test_fenced_transition_persistence_fails_closed_when_record_disappears(
    lifecycle: Lifecycle,
) -> None:
    execution = await _lease(lifecycle)
    request = WorkItemTransitionRequest(
        execution=execution,
        request_id="missing-fenced-transition",
        operation="record_handoff",
        target_status="blocked",
        result_execution_status=WorkItemExecutionStatus.BLOCKED,
    )
    fence = ConversationLifecycleFence(
        conversation_id=ConversationId("conversation-1"),
        identity=ExternalDeliveryIdentity(
            provider=ExternalProvider("linear"),
            route=ExternalRoute("project"),
            delivery_id=ExternalDeliveryId("delivery-1"),
        ),
        claim_id=ConversationClaimId("claim-1"),
        control_state_revision="revision-1",
    )

    with (
        patch(
            "pynchy.state.work_item_transitions.lifecycle_fence_matches",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pynchy.state.work_item_transitions.get_work_item_transition_by_request",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(RuntimeError, match="was not persisted"),
    ):
        await begin_work_item_transition_if_lifecycle_current(request, lifecycle_fence=fence)


async def test_unfenced_transition_resolution_fails_closed_when_execution_disappears(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    transition = await get_work_item_transition_by_request("lease-1")
    assert transition is not None

    with (
        patch(
            "pynchy.state.work_item_transitions._resolve_work_item_transition",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(RuntimeError, match="lost its lifecycle fence"),
    ):
        await resolve_work_item_transition(
            transition=transition,
            execution_status=WorkItemExecutionStatus.IN_PROGRESS,
            transition_status=WorkItemTransitionStatus.SUCCEEDED,
        )


async def test_transition_resolution_fails_closed_when_execution_disappears(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    transition = await get_work_item_transition_by_request("lease-1")
    assert transition is not None

    with (
        patch(
            "pynchy.state.work_item_transitions.get_work_item_execution",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(RuntimeError, match="disappeared during transition resolution"),
    ):
        await resolve_work_item_transition(
            transition=transition,
            execution_status=WorkItemExecutionStatus.IN_PROGRESS,
            transition_status=WorkItemTransitionStatus.SUCCEEDED,
        )


@pytest.mark.parametrize(
    "terminal_status",
    [
        WorkItemExecutionStatus.COMPLETED,
        WorkItemExecutionStatus.CANCELLED,
        WorkItemExecutionStatus.HANDED_OFF,
        WorkItemExecutionStatus.FAILED,
    ],
)
async def test_stale_cancellation_preserves_hard_terminal_execution(
    lifecycle: Lifecycle,
    terminal_status: WorkItemExecutionStatus,
) -> None:
    execution = await _lease(lifecycle)
    transition = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id=f"finish-{terminal_status.value}",
            operation="finish",
            target_status=terminal_status.value,
            result_execution_status=terminal_status,
        )
    )
    terminal = await resolve_work_item_transition(
        transition=transition,
        execution_status=terminal_status,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
    )

    preserved = await cancel_work_item_execution(execution.id, blocker="stale callback")

    assert preserved == terminal


async def test_cancellation_can_still_retire_blocked_execution(lifecycle: Lifecycle) -> None:
    execution = await _lease(lifecycle)
    transition = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="block-1",
            operation="block",
            target_status="blocked",
            result_execution_status=WorkItemExecutionStatus.BLOCKED,
        )
    )
    blocked = await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.BLOCKED,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
    )

    cancelled = await cancel_work_item_execution(blocked.id, blocker="operator reset")

    assert cancelled.status is WorkItemExecutionStatus.CANCELLED
    assert cancelled.blocker == "operator reset"


@pytest.mark.parametrize("with_receipt", [False, True])
async def test_first_settled_transition_resolution_wins_after_unknown(
    lifecycle: Lifecycle,
    with_receipt: bool,
) -> None:
    execution = await _lease(lifecycle)
    transition = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="review-1",
            operation="request_review",
            target_status="awaiting_review",
            result_execution_status=WorkItemExecutionStatus.AWAITING_REVIEW,
        )
    )
    review_issue = (
        {
            **lifecycle.state.issue,
            "identifier": "PYN-99",
            "state": {"id": "state-awaiting-review", "name": "Awaiting Review"},
            "updatedAt": None,
        }
        if with_receipt
        else None
    )
    unresolved = await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.UNKNOWN,
        transition_status=WorkItemTransitionStatus.UNKNOWN,
        error="provider response lost",
    )
    assert unresolved.status is WorkItemExecutionStatus.UNKNOWN
    assert unresolved.observed_updated_at == "2026-07-25T17:00:00+00:00"
    assert unresolved.observed_state_id == "state-in-progress"

    first = await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.AWAITING_REVIEW,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
        issue=review_issue,
    )
    assert first.linear_issue_identifier == ("PYN-99" if with_receipt else "PYN-1")
    assert first.observed_state_id == (
        "state-awaiting-review" if with_receipt else "state-in-progress"
    )
    assert first.observed_updated_at == (None if with_receipt else "2026-07-25T17:00:00+00:00")

    retry = await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.FAILED,
        transition_status=WorkItemTransitionStatus.CONFLICT,
        issue={"malformed": "stale provider payload"},
        error="stale conflict",
    )
    persisted = await get_work_item_transition_by_request("review-1")

    assert retry == first
    assert persisted is not None
    assert persisted.status is WorkItemTransitionStatus.SUCCEEDED
    assert persisted.receipt == review_issue
    assert persisted.error is None


async def test_transition_cannot_resolve_back_to_pending(lifecycle: Lifecycle) -> None:
    execution = await _lease(lifecycle)
    transition = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="invalid-pending",
            operation="request_review",
            target_status="awaiting_review",
            result_execution_status=WorkItemExecutionStatus.AWAITING_REVIEW,
        )
    )

    with pytest.raises(ValueError, match="cannot resolve to pending"):
        await resolve_work_item_transition(
            transition=transition,
            execution_status=WorkItemExecutionStatus.AWAITING_REVIEW,
            transition_status=WorkItemTransitionStatus.PENDING,
        )

    persisted = await get_work_item_transition_by_request("invalid-pending")
    assert persisted is not None
    assert persisted.status is WorkItemTransitionStatus.PENDING


async def test_done_webhook_ignores_unknown_execution_without_done_transition(
    lifecycle: Lifecycle,
) -> None:
    execution = await _lease(lifecycle)
    uncertain_move = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="move-unknown",
            operation="move",
            target_status="awaiting_review",
            result_execution_status=WorkItemExecutionStatus.AWAITING_REVIEW,
        )
    )
    await resolve_work_item_transition(
        transition=uncertain_move,
        execution_status=WorkItemExecutionStatus.UNKNOWN,
        transition_status=WorkItemTransitionStatus.UNKNOWN,
    )

    assert await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1") is None
