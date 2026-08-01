"""Public completion behavior for persisted Linear work-item transitions."""

from __future__ import annotations

from pynchy.plugins.integrations.linear_work_item_completion import (
    complete_reviewed_work_item,
)
from pynchy.state import begin_work_item_transition, resolve_work_item_transition
from pynchy.work_items.api import (
    WorkItemExecutionStatus,
    WorkItemTransitionRequest,
    WorkItemTransitionStatus,
)
from tests.linear_work_items_support import Lifecycle, _lease, _state

pytest_plugins = ("tests.linear_work_items_support",)


async def test_done_webhook_reconciles_an_unknown_execution_with_done_transition(
    lifecycle: Lifecycle,
) -> None:
    execution = await _lease(lifecycle)
    transition = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="unknown-done-transition",
            operation="complete_after_linear_done",
            target_status="done",
            result_execution_status=WorkItemExecutionStatus.COMPLETED,
        )
    )
    await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.UNKNOWN,
        transition_status=WorkItemTransitionStatus.UNKNOWN,
    )
    lifecycle.state.issue["state"] = _state("state-done")

    completed = await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1")

    assert completed is not None
    assert completed.status is WorkItemExecutionStatus.COMPLETED


async def test_done_webhook_reuses_an_existing_completion_transition(
    lifecycle: Lifecycle,
) -> None:
    execution = await _lease(lifecycle)
    await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="linear-review:delivery-1",
            operation="complete_after_linear_done",
            target_status="done",
            result_execution_status=WorkItemExecutionStatus.COMPLETED,
        )
    )
    lifecycle.state.issue["state"] = _state("state-done")

    completed = await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1")

    assert completed is not None
    assert completed.status is WorkItemExecutionStatus.COMPLETED
