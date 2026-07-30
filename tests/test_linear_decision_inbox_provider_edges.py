"""Provider-boundary behavior for the Linear decision inbox."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from pynchy.linear_plan_types import LinearPlanReviewAdmission
from pynchy.plugins.integrations.linear_decision_inbox import (
    process_linear_plan_review_admission,
    reconcile_all_linear_work_items,
    reconcile_linear_decision_inbox,
)
from tests.linear_decision_inbox_support import (
    _board,
    _DecisionClient,
    _Workspace,
)

pytest_plugins = ("tests.linear_decision_inbox_support",)


@dataclass(frozen=True)
class _AccountConfig:
    public_source: bool | str


@dataclass(frozen=True)
class _Account:
    name: str
    config: _AccountConfig


class _ClientContext:
    def __init__(self, client: object) -> None:
        self.client = client

    async def __aenter__(self) -> object:
        return self.client

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


class _MalformedDecisionClient(_DecisionClient):
    def __init__(self, response: object) -> None:
        super().__init__()
        self.response = response

    async def query(self, query: str, **variables: object) -> dict[str, object]:
        if "PynchyLinearDecisionInbox" in query:
            response = self.response
            if not isinstance(response, dict):
                raise AssertionError("test response must be a mapping")
            return response
        return await super().query(query, **variables)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        pytest.param({}, "workflow state was not found", id="missing-workflow-state"),
        pytest.param(
            {"workflowState": []},
            "workflow state was not an object",
            id="non-object-workflow-state",
        ),
        pytest.param(
            {"workflowState": {"issues": []}},
            "workflow state issues were not an object",
            id="non-object-issues-connection",
        ),
        pytest.param(
            {"workflowState": {"issues": {"nodes": {}}}},
            "issue nodes were not an array",
            id="non-array-issue-nodes",
        ),
        pytest.param(
            {"workflowState": {"issues": {"nodes": [], "pageInfo": []}}},
            "pageInfo was not an object",
            id="non-object-page-info",
        ),
        pytest.param(
            {"workflowState": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": "yes"}}}},
            "pagination flag was not boolean",
            id="non-boolean-pagination",
        ),
        pytest.param(
            {"workflowState": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": True}}}},
            "pagination cursor was invalid",
            id="missing-pagination-cursor",
        ),
    ],
)
async def test_decision_inbox_rejects_malformed_provider_payloads(
    response: object,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        await reconcile_linear_decision_inbox(
            _MalformedDecisionClient(response),
            [_Workspace("beta", "Beta", "linear:beta")],
            {"beta": _board("project-beta")},
        )


async def test_decision_inbox_with_no_boards_does_not_query_provider() -> None:
    client = AsyncMock(spec=_DecisionClient)

    created = await reconcile_linear_decision_inbox(client, [], {})

    assert created == []
    client.query.assert_not_awaited()


async def test_decision_inbox_ignores_boards_without_matching_workspace_projects() -> None:
    client = _DecisionClient()

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-unlisted")},
    )

    assert created == []


async def test_plan_review_rejects_a_workspace_that_was_removed() -> None:
    admission = LinearPlanReviewAdmission(
        workspace="removed",
        issue_id="issue-1",
        identifier="SYN-1",
        updated_at="2026-07-19T08:00:00+00:00",
        public_source=True,
    )

    with pytest.raises(ValueError, match="workspace is no longer configured"):
        await process_linear_plan_review_admission(
            admission,
            [],
            review_plan=AsyncMock(),
            broadcast_host_message=AsyncMock(),
        )


async def test_reconcile_all_skips_workspaces_without_a_configured_account(monkeypatch) -> None:
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_account_for_workspace",
        lambda _workspace: None,
    )

    admitted = await reconcile_all_linear_work_items(
        {"beta": _Workspace("beta", "Beta", "linear:beta")},
        {"beta": _board("project-beta")},
        review_plan=AsyncMock(),
        broadcast_host_message=AsyncMock(),
        defer_plan_review=AsyncMock(),
    )

    assert admitted == []


async def test_reconcile_all_runs_provider_recovery_before_inbox_admission(monkeypatch) -> None:
    account = _Account("primary", _AccountConfig(public_source=False))
    client = object()
    provider_recovery = AsyncMock(return_value=2)
    inbox = AsyncMock(return_value=[])
    client_context = _ClientContext(client)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_account_for_workspace",
        lambda _workspace: account,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_client",
        lambda *, workspace: client_context,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.reconcile_provider_work_item_state",
        provider_recovery,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.reconcile_linear_decision_inbox",
        inbox,
    )

    admitted = await reconcile_all_linear_work_items(
        {"beta": _Workspace("beta", "Beta", "linear:beta")},
        {"beta": _board("project-beta")},
        review_plan=AsyncMock(),
        broadcast_host_message=AsyncMock(),
        defer_plan_review=AsyncMock(),
    )

    assert admitted == []
    provider_recovery.assert_awaited_once_with(client, {"beta": _board("project-beta")})
    inbox.assert_awaited_once()
    assert inbox.await_args.kwargs["public_source"] is False
