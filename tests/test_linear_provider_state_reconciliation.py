"""Recovery tests for Linear state changes missed while callbacks are offline."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_provider_reconciliation import (
    LinearDecisionInboxRuntime,
    UnavailableExecutionProbe,
    configure_linear_decision_inbox_runtime,
    reconcile_provider_work_item_state,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearWorkItemRuntime,
    configure_linear_work_item_runtime,
    reconcile_work_item,
)
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransition,
    WorkItemTransitionStatus,
)


def _state(name: str) -> dict[str, str]:
    slug = name.casefold().replace(" ", "-")
    return {"id": f"state-{slug}", "name": name}


def _board() -> LinearWorkspaceBoard:
    return LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "in_progress": _state("In Progress"),
            "human_approved": _state("Human Approved"),
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


def _transition(
    *,
    status: WorkItemTransitionStatus,
    created_at: str = "2020-01-01T00:00:00+00:00",
) -> WorkItemTransition:
    return WorkItemTransition(
        id=1,
        execution_id="execution-1",
        request_id="request-1",
        operation="move",
        target_status="awaiting_review",
        result_execution_status=WorkItemExecutionStatus.AWAITING_REVIEW,
        evidence_refs=(),
        summary=None,
        blocker=None,
        handoff_to=None,
        status=status,
        receipt=None,
        error=None,
        created_at=created_at,
        resolved_at=None,
    )


class _Client(LinearClient):
    team_key = "SYN"

    def __init__(self, issue: dict[str, object] | None) -> None:
        self.issue = issue

    async def get_issue(self, _issue_id: str) -> dict[str, object] | None:
        return self.issue

    async def query(self, _query: str, **_variables: object) -> dict[str, object]:
        raise AssertionError("provider reconciliation does not scan state inboxes")

    async def create_comment(self, _issue_id: str, _body: str) -> dict[str, object]:
        raise AssertionError("provider reconciliation does not comment")


def _configure_runtime(
    execution: WorkItemExecution,
    *,
    transition: WorkItemTransition | None = None,
    cancel: AsyncMock | None = None,
    retire: AsyncMock | None = None,
    retire_terminal: AsyncMock | None = None,
) -> None:
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[execution]),
            list_terminal_repair_candidates=AsyncMock(return_value=[]),
            get_latest_unresolved_transition=AsyncMock(return_value=transition),
            cancel_execution=cancel or AsyncMock(),
            retire_execution=retire or AsyncMock(),
            retire_terminal_execution_if_unowned=AsyncMock(return_value=False),
            retire_terminal_execution=retire_terminal or AsyncMock(),
        )
    )


@pytest.mark.asyncio
async def test_done_state_completes_retired_workspace_execution(monkeypatch) -> None:
    execution = replace(_execution(), workspace="retired-pynchy")
    completed = replace(execution, status=WorkItemExecutionStatus.COMPLETED)
    retire = AsyncMock()
    retire_terminal = AsyncMock()
    cancel = AsyncMock()
    _configure_runtime(
        execution,
        cancel=cancel,
        retire=retire,
        retire_terminal=retire_terminal,
    )
    complete = AsyncMock(return_value=completed)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.complete_reviewed_work_item",
        complete,
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("Done"),
        "project": {"id": "project-1"},
    }

    retired = await reconcile_provider_work_item_state(
        _Client(issue),
        {"pynchy": _board()},
    )

    assert retired == 1
    complete.assert_awaited_once_with(
        "retired-pynchy",
        "issue-1",
        "reconcile:execution-1:2026-07-29T01:00:00Z",
        controller_workspace="pynchy",
    )
    retire.assert_not_awaited()
    retire_terminal.assert_awaited_once_with(completed, "2026-07-29T01:00:00Z")
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
    retire_terminal = AsyncMock()
    cancel = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[old_execution, current_execution]),
            list_terminal_repair_candidates=AsyncMock(return_value=[]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=cancel,
            retire_execution=retire,
            retire_terminal_execution_if_unowned=AsyncMock(return_value=False),
            retire_terminal_execution=retire_terminal,
        )
    )
    complete = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.complete_reviewed_work_item",
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
    retire_terminal = AsyncMock()
    _configure_runtime(
        execution,
        cancel=cancel,
        retire=retire,
        retire_terminal=retire_terminal,
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("Awaiting Plan Approval"),
        "project": {"id": "project-1"},
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
    retire_terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_matching_provider_state_preserves_active_runtime() -> None:
    execution = _execution()
    cancel = AsyncMock()
    retire = AsyncMock()
    retire_terminal = AsyncMock()
    _configure_runtime(
        execution,
        cancel=cancel,
        retire=retire,
        retire_terminal=retire_terminal,
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("In Progress"),
        "project": {"id": "project-1"},
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
    transition = _transition(status=WorkItemTransitionStatus.UNKNOWN)
    latest = AsyncMock(return_value=transition)
    cancel = AsyncMock()
    retire = AsyncMock()
    retire_terminal = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[execution]),
            list_terminal_repair_candidates=AsyncMock(return_value=[]),
            get_latest_unresolved_transition=latest,
            cancel_execution=cancel,
            retire_execution=retire,
            retire_terminal_execution_if_unowned=AsyncMock(return_value=False),
            retire_terminal_execution=retire_terminal,
        )
    )
    reconcile = AsyncMock(
        return_value=replace(execution, status=WorkItemExecutionStatus.IN_PROGRESS)
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.reconcile_work_item",
        reconcile,
    )
    client = _Client(
        {
            "id": "issue-1",
            "updatedAt": "2026-07-29T01:00:00Z",
            "state": _state("In Progress"),
            "project": {"id": "project-1"},
        }
    )

    retired = await reconcile_provider_work_item_state(client, {"pynchy": _board()})

    assert retired == 0
    latest.assert_awaited_once_with(execution.id)
    reconcile.assert_awaited_once_with(client, "pynchy", "issue-1", transition)
    cancel.assert_not_awaited()
    retire.assert_not_awaited()


async def test_aged_pending_transition_settles_before_provider_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    transition = _transition(status=WorkItemTransitionStatus.PENDING)
    reconcile = AsyncMock(
        return_value=replace(execution, status=WorkItemExecutionStatus.AWAITING_REVIEW)
    )
    retire = AsyncMock()
    _configure_runtime(execution, transition=transition, retire=retire)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.reconcile_work_item",
        reconcile,
    )
    client = _Client(
        {
            "id": "issue-1",
            "updatedAt": "2026-07-29T01:00:00Z",
            "state": _state("Awaiting Review"),
            "project": {"id": "project-1"},
        }
    )

    assert await reconcile_provider_work_item_state(client, {"pynchy": _board()}) == 0
    reconcile.assert_awaited_once_with(client, "pynchy", "issue-1", transition)
    retire.assert_not_awaited()


async def test_fresh_pending_transition_remains_owned_by_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    transition = _transition(
        status=WorkItemTransitionStatus.PENDING,
        created_at=datetime.now(UTC).isoformat(),
    )
    reconcile = AsyncMock()
    _configure_runtime(execution, transition=transition)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.reconcile_work_item",
        reconcile,
    )
    client = _Client(
        {
            "id": "issue-1",
            "updatedAt": "2026-07-29T01:00:00Z",
            "state": _state("Awaiting Review"),
            "project": {"id": "project-1"},
        }
    )

    assert await reconcile_provider_work_item_state(client, {"pynchy": _board()}) == 0
    reconcile.assert_not_awaited()


async def test_pending_claim_mismatch_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    failed = replace(execution, status=WorkItemExecutionStatus.FAILED)
    transition = replace(
        _transition(status=WorkItemTransitionStatus.PENDING),
        operation="claim",
        target_status="in_progress",
        result_execution_status=WorkItemExecutionStatus.IN_PROGRESS,
    )
    resolve = AsyncMock(return_value=failed)
    transition_issue = AsyncMock()
    configure_linear_work_item_runtime(
        LinearWorkItemRuntime(
            get_transition_by_request=AsyncMock(),
            get_execution=AsyncMock(return_value=execution),
            get_active_execution=AsyncMock(),
            create_claim=AsyncMock(),
            begin_transition=AsyncMock(),
            resolve_transition=resolve,
            resolve_transition_if_lifecycle_current=AsyncMock(),
        )
    )
    issue = {
        "id": "issue-1",
        "identifier": "SYN-1",
        "url": "https://linear.app/example/issue/SYN-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("Human Approved"),
        "project": {"id": "project-1"},
    }
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider.workspace_issue",
        AsyncMock(return_value=(issue, _board())),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider.transition_issue",
        transition_issue,
    )

    assert (
        await reconcile_work_item(
            _Client(issue),
            "pynchy",
            "issue-1",
            transition,
        )
        is failed
    )
    transition_issue.assert_not_awaited()
    assert resolve.await_args.kwargs["transition_status"] is WorkItemTransitionStatus.CONFLICT


async def test_retired_workspace_execution_reconciles_against_current_project() -> None:
    execution = replace(_execution(), workspace="retired-pynchy")
    cancelled = replace(execution, status=WorkItemExecutionStatus.CANCELLED)
    list_executions = AsyncMock(return_value=[execution])
    cancel = AsyncMock(return_value=cancelled)
    retire = AsyncMock()
    retire_terminal = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=list_executions,
            list_terminal_repair_candidates=AsyncMock(return_value=[]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=cancel,
            retire_execution=retire,
            retire_terminal_execution_if_unowned=AsyncMock(return_value=False),
            retire_terminal_execution=retire_terminal,
        )
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("Rejected"),
        "project": {"id": "project-1"},
    }

    retired = await reconcile_provider_work_item_state(
        _Client(issue),
        {"pynchy": _board()},
    )

    assert retired == 1
    list_executions.assert_awaited_once_with(limit=None)
    cancel.assert_awaited_once_with(
        execution.id,
        blocker="Linear state no longer authorizes this execution: Rejected",
    )
    retire.assert_awaited_once_with(cancelled)
    retire_terminal.assert_not_awaited()


async def test_issue_moved_outside_managed_projects_cancels_old_owner() -> None:
    execution = _execution()
    cancelled = replace(execution, status=WorkItemExecutionStatus.CANCELLED)
    cancel = AsyncMock(return_value=cancelled)
    retire = AsyncMock()
    retire_terminal = AsyncMock()
    _configure_runtime(
        execution,
        cancel=cancel,
        retire=retire,
        retire_terminal=retire_terminal,
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("In Progress"),
        "project": {"id": "unmanaged-project"},
    }

    assert (
        await reconcile_provider_work_item_state(
            _Client(issue),
            {"pynchy": _board()},
        )
        == 1
    )
    cancel.assert_awaited_once_with(
        execution.id,
        blocker="Linear state no longer authorizes this execution: issue left managed projects",
    )
    retire.assert_awaited_once_with(cancelled)
    retire_terminal.assert_not_awaited()


async def test_deleted_managed_issue_retires_only_the_execution_runtime() -> None:
    execution = _execution()
    cancelled = replace(execution, status=WorkItemExecutionStatus.CANCELLED)
    cancel = AsyncMock(return_value=cancelled)
    retire = AsyncMock()
    retire_terminal = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[execution]),
            list_terminal_repair_candidates=AsyncMock(return_value=[]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=cancel,
            retire_execution=retire,
            retire_terminal_execution_if_unowned=AsyncMock(return_value=False),
            retire_terminal_execution=retire_terminal,
        )
    )

    assert (
        await reconcile_provider_work_item_state(
            _Client(None),
            {"pynchy": _board()},
        )
        == 1
    )
    retire.assert_awaited_once_with(cancelled)
    retire_terminal.assert_not_awaited()


async def test_missing_issue_without_current_account_binding_records_negative_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = replace(_execution(), workspace="removed-workspace")
    cancel = AsyncMock()
    probes: dict[str, UnavailableExecutionProbe] = {}
    _configure_runtime(execution, cancel=cancel)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_provider_reconciliation.linear_account_for_workspace",
        lambda _workspace: None,
    )

    assert (
        await reconcile_provider_work_item_state(
            _Client(None),
            {"pynchy": _board()},
            account_name="primary",
            unavailable_probes=probes,
        )
        == 0
    )
    cancel.assert_not_awaited()
    assert probes[execution.id].execution is execution
    assert probes[execution.id].account_names == {"primary"}


async def test_provider_cancelled_state_uses_terminal_runtime_retirement() -> None:
    execution = _execution()
    cancelled = replace(execution, status=WorkItemExecutionStatus.CANCELLED)
    cancel = AsyncMock(return_value=cancelled)
    retire = AsyncMock()
    retire_terminal = AsyncMock()
    _configure_runtime(
        execution,
        cancel=cancel,
        retire=retire,
        retire_terminal=retire_terminal,
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": {**_state("Cancelled"), "type": "canceled"},
        "project": {"id": "project-1"},
    }

    assert (
        await reconcile_provider_work_item_state(
            _Client(issue),
            {"pynchy": _board()},
        )
        == 1
    )
    retire.assert_not_awaited()
    retire_terminal.assert_awaited_once_with(cancelled, "2026-07-29T01:00:00Z")


async def test_concurrent_terminal_execution_is_not_retired_as_this_cancellation() -> None:
    execution = _execution()
    completed = replace(execution, status=WorkItemExecutionStatus.COMPLETED)
    retire = AsyncMock()
    retire_terminal = AsyncMock()
    _configure_runtime(
        execution,
        cancel=AsyncMock(return_value=completed),
        retire=retire,
        retire_terminal=retire_terminal,
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": {**_state("Cancelled"), "type": "canceled"},
        "project": {"id": "project-1"},
    }

    assert (
        await reconcile_provider_work_item_state(
            _Client(issue),
            {"pynchy": _board()},
        )
        == 0
    )
    retire.assert_not_awaited()
    retire_terminal.assert_not_awaited()


async def test_provider_transition_in_flight_preserves_active_runtime() -> None:
    execution = _execution()
    cancel = AsyncMock()
    retire = AsyncMock()
    retire_terminal = AsyncMock()
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=AsyncMock(return_value=[execution]),
            list_terminal_repair_candidates=AsyncMock(return_value=[]),
            get_latest_unresolved_transition=AsyncMock(return_value=None),
            cancel_execution=cancel,
            retire_execution=retire,
            retire_terminal_execution_if_unowned=AsyncMock(return_value=False),
            retire_terminal_execution=retire_terminal,
        )
    )
    issue = {
        "id": "issue-1",
        "updatedAt": "2026-07-29T01:00:00Z",
        "state": _state("Awaiting Review"),
        "project": {"id": "project-1"},
    }

    retired = await reconcile_provider_work_item_state(
        _Client(issue),
        {"pynchy": _board()},
    )

    assert retired == 0
    cancel.assert_not_awaited()
    retire.assert_not_awaited()
