"""Recovery tests for Linear state changes missed while callbacks are offline."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_decision_inbox import (
    LinearDecisionInboxRuntime,
    configure_linear_decision_inbox_runtime,
    reconcile_provider_work_item_state,
)
from pynchy.work_items.api import WorkItemExecution, WorkItemExecutionStatus


def _state(name: str) -> dict[str, str]:
    slug = name.casefold().replace(" ", "-")
    return {"id": f"state-{slug}", "name": name}


def _board() -> LinearWorkspaceBoard:
    return LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "in_progress": _state("In Progress"),
            "awaiting_review": _state("Awaiting Review"),
            "follow_ups": _state("Follow Ups"),
            "blocked": _state("Blocked"),
            "done": _state("Done"),
        },
    )


def _execution() -> WorkItemExecution:
    return WorkItemExecution(
        id="execution-1",
        workspace="pynchy",
        linear_issue_id="issue-1",
        linear_issue_identifier="SYN-1",
        linear_issue_url="https://linear.app/example/issue/SYN-1",
        turn_id="turn-1",
        task_id="task-1",
        attempt=1,
        flow_id="flow-1",
        temporal_workflow_id="workflow-1",
        initiated_by="controller",
        observed_state_id="state-in-progress",
        observed_state_name="In Progress",
        observed_updated_at="2026-07-29T00:00:00Z",
        status=WorkItemExecutionStatus.IN_PROGRESS,
        summary="Delivered",
        blocker=None,
        handoff_to=None,
        evidence_refs=("commit:abc",),
        requester_delivery_status="not_required",
        requester_delivery_turn_id=None,
        requester_delivery_error=None,
        requester_delivered_at=None,
        created_at="2026-07-29T00:00:00Z",
        updated_at="2026-07-29T00:00:00Z",
        completed_at=None,
    )


class _Client:
    def __init__(self, issue: dict[str, object] | None) -> None:
        self.issue = issue

    async def get_issue(self, _issue_id: str) -> dict[str, object] | None:
        return self.issue

    async def query(self, _query: str, **_variables: object) -> dict[str, object]:
        raise AssertionError("provider reconciliation does not scan state inboxes")

    async def create_comment(self, _issue_id: str, _body: str) -> dict[str, object]:
        raise AssertionError("provider reconciliation does not comment")


@pytest.mark.asyncio
async def test_done_state_completes_and_retires_exact_runtime(monkeypatch) -> None:
    execution = _execution()
    completed = replace(execution, status=WorkItemExecutionStatus.COMPLETED)
    retire = AsyncMock()
    cancel = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[execution]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=cancel,
            retire_execution=retire,
        )
    )
    complete = AsyncMock(return_value=completed)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.complete_reviewed_work_item",
        complete,
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("Done"),
    }

    retired = await reconcile_provider_work_item_state(
        _Client(issue),
        {"pynchy": _board()},
    )

    assert retired == 1
    complete.assert_awaited_once_with(
        "pynchy",
        "issue-1",
        "reconcile:execution-1:2026-07-29T01:00:00Z",
    )
    retire.assert_awaited_once_with(completed)
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_superseded_execution_is_not_reconciled(monkeypatch) -> None:
    old_execution = replace(
        _execution(),
        status=WorkItemExecutionStatus.AWAITING_REVIEW,
    )
    current_execution = replace(
        _execution(),
        id="execution-2",
        attempt=2,
        status=WorkItemExecutionStatus.COMPLETED,
    )
    retire = AsyncMock()
    cancel = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[old_execution, current_execution]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=cancel,
            retire_execution=retire,
        )
    )
    complete = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.complete_reviewed_work_item",
        complete,
    )

    retired = await reconcile_provider_work_item_state(
        _Client(
            {
                "id": "issue-1",
                "updatedAt": "2026-07-29T01:00:00Z",
                "state": _state("Done"),
            }
        ),
        {"pynchy": _board()},
    )

    assert retired == 0
    complete.assert_not_awaited()
    cancel.assert_not_awaited()
    retire.assert_not_awaited()


@pytest.mark.asyncio
async def test_deauthorized_state_cancels_without_changing_provider() -> None:
    execution = _execution()
    cancelled = replace(execution, status=WorkItemExecutionStatus.CANCELLED)
    cancel = AsyncMock(return_value=cancelled)
    retire = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[execution]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=cancel,
            retire_execution=retire,
        )
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("Awaiting Plan Approval"),
    }

    retired = await reconcile_provider_work_item_state(
        _Client(issue),
        {"pynchy": _board()},
    )

    assert retired == 1
    cancel.assert_awaited_once_with(
        "execution-1",
        blocker="Linear state no longer authorizes this execution: Awaiting Plan Approval",
    )
    retire.assert_awaited_once_with(cancelled)


@pytest.mark.asyncio
async def test_matching_provider_state_preserves_active_runtime() -> None:
    execution = _execution()
    cancel = AsyncMock()
    retire = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[execution]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=cancel,
            retire_execution=retire,
        )
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("In Progress"),
    }

    retired = await reconcile_provider_work_item_state(
        _Client(issue),
        {"pynchy": _board()},
    )

    assert retired == 0
    cancel.assert_not_awaited()
    retire.assert_not_awaited()


async def test_uncertain_execution_reconciles_before_provider_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = replace(_execution(), status=WorkItemExecutionStatus.UNKNOWN)
    transition = object()
    latest = AsyncMock(return_value=transition)
    cancel = AsyncMock()
    retire = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[execution]),
            get_latest_unresolved_transition=latest,
            cancel_execution=cancel,
            retire_execution=retire,
        )
    )
    reconcile = AsyncMock(
        return_value=replace(execution, status=WorkItemExecutionStatus.IN_PROGRESS)
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.reconcile_work_item",
        reconcile,
    )
    client = _Client(
        {
            "id": "issue-1",
            "updatedAt": "2026-07-29T01:00:00Z",
            "state": _state("In Progress"),
        }
    )

    retired = await reconcile_provider_work_item_state(client, {"pynchy": _board()})

    assert retired == 0
    latest.assert_awaited_once_with(execution.id)
    reconcile.assert_awaited_once_with(client, "pynchy", "issue-1", transition)
    cancel.assert_not_awaited()
    retire.assert_not_awaited()


async def test_provider_transition_in_flight_preserves_active_runtime() -> None:
    execution = _execution()
    cancel = AsyncMock()
    retire = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[execution]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=cancel,
            retire_execution=retire,
        )
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("Awaiting Review"),
    }

    retired = await reconcile_provider_work_item_state(
        _Client(issue),
        {"pynchy": _board()},
    )

    assert retired == 0
    cancel.assert_not_awaited()
    retire.assert_not_awaited()
