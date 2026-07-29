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
from pynchy.state import (
    begin_work_item_transition,
    begin_work_item_transition_if_lifecycle_current,
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
    await _lease(lifecycle)
    transition = await get_work_item_transition_by_request("lease-1")
    assert transition is not None

    with pytest.raises(error, match=message):
        await resolve_work_item_transition(
            transition=transition,
            execution_status=WorkItemExecutionStatus.IN_PROGRESS,
            transition_status=WorkItemTransitionStatus.SUCCEEDED,
            issue=issue,
        )


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
