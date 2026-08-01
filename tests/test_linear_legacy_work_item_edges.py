"""Public failure behavior for legacy Linear lease adoption."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_legacy_work_items import (
    LegacyAdoptionRequest,
    LinearLegacyWorkItemRuntime,
    adopt_legacy_in_progress_execution,
    configure_linear_legacy_work_item_runtime,
)
from pynchy.work_items.api import (
    WorkItemClaimConflictError,
    WorkItemExecution,
    WorkItemExecutionStatus,
)
from tests.linear_decision_inbox_support import _board, _Workspace


@dataclass(frozen=True)
class _Issue:
    id: str = "issue-1"
    identifier: str = "SYN-1"


@dataclass(frozen=True)
class _Task:
    id: str = "linear-ready-for-planning-syn-1-proof"
    status: str = "completed"
    prompt: str = '[Source: linear-decision-inbox] {"issue_id": "issue-1"}'


@dataclass(frozen=True)
class _Transition:
    execution_id: str


class _Client:
    def __init__(self, issue: dict[str, object] | None) -> None:
        self.issue = issue

    async def get_issue(self, _issue_id: str) -> dict[str, object] | None:
        return self.issue


def _execution(*, workspace: str = "other") -> WorkItemExecution:
    return WorkItemExecution(
        id="execution-1",
        workspace=workspace,
        linear_issue_id="issue-1",
        linear_issue_identifier="SYN-1",
        linear_issue_url="https://linear.example/SYN-1",
        turn_id=None,
        task_id=None,
        attempt=1,
        flow_id=None,
        temporal_workflow_id=None,
        initiated_by="test",
        observed_state_id="state-progress",
        observed_state_name="In Progress",
        observed_updated_at=None,
        status=WorkItemExecutionStatus.IN_PROGRESS,
        summary=None,
        blocker=None,
        handoff_to=None,
        evidence_refs=(),
        requester_delivery_status="not_requested",
        requester_delivery_turn_id=None,
        requester_delivery_error=None,
        requester_delivered_at=None,
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
        completed_at=None,
    )


def _request(
    *,
    board: LinearWorkspaceBoard | None = None,
    issue: _Issue | None = None,
    provider_issue: dict[str, object] | None = None,
) -> LegacyAdoptionRequest:
    tasks = AsyncMock(return_value=[_Task()])
    configure_linear_legacy_work_item_runtime(
        LinearLegacyWorkItemRuntime(
            get_all_tasks=tasks,
            get_transition_by_request=AsyncMock(return_value=None),
            create_claim=AsyncMock(),
            claim_request=lambda **kwargs: kwargs,
            get_active_execution=AsyncMock(return_value=None),
            get_execution=AsyncMock(return_value=None),
            resolve_transition=AsyncMock(return_value=_execution(workspace="beta")),
        )
    )
    return LegacyAdoptionRequest(
        client=_Client(provider_issue),
        issue=issue or _Issue(),
        workspace=_Workspace("beta", "Beta", "linear:beta"),
        board=board or _board("project-beta"),
    )


@pytest.mark.asyncio
async def test_adoption_ignores_provider_issue_not_in_progress() -> None:
    request = _request(
        provider_issue={"state": {"id": "state-approved"}},
    )

    assert await adopt_legacy_in_progress_execution(request) is None


@pytest.mark.asyncio
async def test_adoption_rejects_a_board_without_the_in_progress_state() -> None:
    request = _request(
        board=LinearWorkspaceBoard(team={}, project={}, states={}),
        provider_issue={"state": {"id": "state-progress"}},
    )

    with pytest.raises(ValueError, match="lacks state in_progress"):
        await adopt_legacy_in_progress_execution(request)


@pytest.mark.asyncio
async def test_adoption_rejects_a_non_text_in_progress_state_id() -> None:
    request = _request(
        board=LinearWorkspaceBoard(team={}, project={}, states={"in_progress": {"id": 42}}),
        provider_issue={"state": {"id": "state-progress"}},
    )

    with pytest.raises(TypeError, match="lacks a text ID"):
        await adopt_legacy_in_progress_execution(request)


@pytest.mark.asyncio
async def test_adoption_re_raises_a_claim_conflict_owned_by_another_workspace() -> None:
    conflicting = _execution(workspace="alpha")
    create_claim = AsyncMock(side_effect=WorkItemClaimConflictError(conflicting))
    configure_linear_legacy_work_item_runtime(
        LinearLegacyWorkItemRuntime(
            get_all_tasks=AsyncMock(return_value=[_Task()]),
            get_transition_by_request=AsyncMock(return_value=None),
            create_claim=create_claim,
            claim_request=lambda **kwargs: kwargs,
            get_active_execution=AsyncMock(return_value=conflicting),
            get_execution=AsyncMock(return_value=None),
            resolve_transition=AsyncMock(),
        )
    )
    request = LegacyAdoptionRequest(
        client=_Client({"state": {"id": "state-progress"}}),
        issue=_Issue(),
        workspace=_Workspace("beta", "Beta", "linear:beta"),
        board=_board("project-beta"),
    )

    with pytest.raises(WorkItemClaimConflictError):
        await adopt_legacy_in_progress_execution(request)

    create_claim.assert_awaited_once()


@pytest.mark.asyncio
async def test_adoption_continues_when_a_claim_conflict_is_owned_by_same_workspace() -> None:
    existing = _execution(workspace="beta")
    transition = _Transition(execution_id=existing.id)
    create_claim = AsyncMock(side_effect=WorkItemClaimConflictError(existing))
    get_transition = AsyncMock(side_effect=[None, transition])
    adopted = _execution(workspace="beta")
    configure_linear_legacy_work_item_runtime(
        LinearLegacyWorkItemRuntime(
            get_all_tasks=AsyncMock(return_value=[_Task()]),
            get_transition_by_request=get_transition,
            create_claim=create_claim,
            claim_request=lambda **kwargs: kwargs,
            get_active_execution=AsyncMock(return_value=existing),
            get_execution=AsyncMock(return_value=None),
            resolve_transition=AsyncMock(return_value=adopted),
        )
    )
    request = LegacyAdoptionRequest(
        client=_Client({"state": {"id": "state-progress"}}),
        issue=_Issue(),
        workspace=_Workspace("beta", "Beta", "linear:beta"),
        board=_board("project-beta"),
    )

    assert await adopt_legacy_in_progress_execution(request) is adopted
    create_claim.assert_awaited_once()


@pytest.mark.asyncio
async def test_adoption_reuses_a_persisted_transition() -> None:
    transition = _Transition(execution_id="existing-execution")
    prior = _execution(workspace="beta")
    adopted = _execution(workspace="beta")
    configure_linear_legacy_work_item_runtime(
        LinearLegacyWorkItemRuntime(
            get_all_tasks=AsyncMock(return_value=[_Task()]),
            get_transition_by_request=AsyncMock(return_value=transition),
            create_claim=AsyncMock(),
            claim_request=lambda **kwargs: kwargs,
            get_active_execution=AsyncMock(return_value=None),
            get_execution=AsyncMock(return_value=prior),
            resolve_transition=AsyncMock(return_value=adopted),
        )
    )
    request = LegacyAdoptionRequest(
        client=_Client({"state": {"id": "state-progress"}}),
        issue=_Issue(),
        workspace=_Workspace("beta", "Beta", "linear:beta"),
        board=_board("project-beta"),
    )

    assert await adopt_legacy_in_progress_execution(request) is adopted


@pytest.mark.asyncio
async def test_adoption_rejects_a_claim_without_a_persisted_transition() -> None:
    get_transition = AsyncMock(side_effect=[None, None])
    configure_linear_legacy_work_item_runtime(
        LinearLegacyWorkItemRuntime(
            get_all_tasks=AsyncMock(return_value=[_Task()]),
            get_transition_by_request=get_transition,
            create_claim=AsyncMock(),
            claim_request=lambda **kwargs: kwargs,
            get_active_execution=AsyncMock(return_value=None),
            get_execution=AsyncMock(return_value=None),
            resolve_transition=AsyncMock(),
        )
    )
    request = LegacyAdoptionRequest(
        client=_Client({"state": {"id": "state-progress"}}),
        issue=_Issue(),
        workspace=_Workspace("beta", "Beta", "linear:beta"),
        board=_board("project-beta"),
    )

    with pytest.raises(RuntimeError, match="was not persisted"):
        await adopt_legacy_in_progress_execution(request)


@pytest.mark.asyncio
async def test_adoption_rejects_a_transition_that_lost_its_execution() -> None:
    transition = _Transition(execution_id="missing-execution")
    configure_linear_legacy_work_item_runtime(
        LinearLegacyWorkItemRuntime(
            get_all_tasks=AsyncMock(return_value=[_Task()]),
            get_transition_by_request=AsyncMock(return_value=transition),
            create_claim=AsyncMock(),
            claim_request=lambda **kwargs: kwargs,
            get_active_execution=AsyncMock(return_value=None),
            get_execution=AsyncMock(return_value=None),
            resolve_transition=AsyncMock(),
        )
    )
    request = LegacyAdoptionRequest(
        client=_Client({"state": {"id": "state-progress"}}),
        issue=_Issue(),
        workspace=_Workspace("beta", "Beta", "linear:beta"),
        board=_board("project-beta"),
    )

    with pytest.raises(RuntimeError, match="lost its execution"):
        await adopt_legacy_in_progress_execution(request)
