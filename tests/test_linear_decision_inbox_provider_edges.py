"""Provider-boundary behavior for the Linear decision inbox."""

from __future__ import annotations

from asyncio import sleep
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from pynchy.linear_plan_types import LinearPlanReviewAdmission
from pynchy.plugins.integrations.linear_decision_inbox import (
    process_linear_plan_review_admission,
    reconcile_all_linear_work_items,
    reconcile_linear_decision_inbox,
)
from pynchy.plugins.integrations.linear_provider_reconciliation import (
    UnavailableExecutionProbe,
)
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.work_items.api import WorkItemExecution, WorkItemExecutionStatus
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


def _execution() -> WorkItemExecution:
    return WorkItemExecution(
        id="execution-1",
        workspace="removed",
        linear_issue_id="issue-1",
        linear_issue_identifier="SYN-1",
        linear_issue_url="https://linear.app/example/issue/SYN-1",
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
        created_at="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00",
        completed_at=None,
    )


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
        {
            "beta": _board("project-unlisted"),
            "removed": _board("project-removed"),
        },
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


async def test_reconcile_all_skips_forbidden_accounts(monkeypatch) -> None:
    account = _Account("primary", _AccountConfig(public_source="forbidden"))
    provider_recovery = AsyncMock()
    inbox = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_account_for_workspace",
        lambda _workspace: account,
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
    provider_recovery.assert_not_awaited()
    inbox.assert_not_awaited()


async def test_reconcile_all_runs_provider_recovery_before_inbox_admission(monkeypatch) -> None:
    account = _Account("primary", _AccountConfig(public_source=False))
    client = object()
    provider_recovery = AsyncMock(return_value=2)
    admitted_task = ScheduledTask(
        id="task-1",
        group_folder="beta",
        chat_jid="linear:beta",
        prompt="prompt",
        schedule_type="once",
        schedule_value="2026-07-19T08:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
    )
    inbox = AsyncMock(return_value=[admitted_task])
    client_context = _ClientContext(client)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_account_for_workspace",
        lambda _workspace: account,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_client",
        lambda *, account_name: client_context,
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

    assert admitted == [admitted_task]
    provider_recovery.assert_awaited_once()
    assert provider_recovery.await_args.args == (client, {"beta": _board("project-beta")})
    assert provider_recovery.await_args.kwargs["account_name"] == "primary"
    inbox.assert_awaited_once()
    assert inbox.await_args.kwargs["public_source"] is False


async def test_reconcile_all_isolates_a_provider_failure(monkeypatch) -> None:
    account = _Account("primary", _AccountConfig(public_source=True))
    secondary = _Account("secondary", _AccountConfig(public_source=True))
    recovered = AsyncMock(return_value=0)
    secondary_client = object()

    class FailingClientContext:
        async def __aenter__(self) -> object:
            raise RuntimeError("provider unavailable")

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

    def failing_client(*, account_name: object) -> FailingClientContext | _ClientContext:
        if account_name == "secondary":
            return _ClientContext(secondary_client)
        return FailingClientContext()

    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.configured_linear_accounts",
        lambda: (account, secondary),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.reconcile_provider_work_item_state",
        recovered,
    )

    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_account_for_workspace",
        lambda _workspace: account,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_client",
        failing_client,
    )

    with pytest.raises(ExceptionGroup, match="Linear account reconciliation failed"):
        await reconcile_all_linear_work_items(
            {"beta": _Workspace("beta", "Beta", "linear:beta")},
            {"beta": _board("project-beta")},
            review_plan=AsyncMock(),
            broadcast_host_message=AsyncMock(),
            defer_plan_review=AsyncMock(),
        )

    recovered.assert_awaited_once()
    assert recovered.await_args.args[0] is secondary_client


async def test_removed_workspace_issue_retires_only_after_every_account_reports_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = (
        _Account("primary", _AccountConfig(public_source=True)),
        _Account("secondary", _AccountConfig(public_source=True)),
    )
    execution = _execution()
    clients = {account.name: object() for account in accounts}
    retire = AsyncMock(return_value=True)

    async def reconcile(
        _client: object,
        _boards: object,
        *,
        account_name: str,
        unavailable_probes: dict[str, UnavailableExecutionProbe],
    ) -> int:
        await sleep(0)
        probe = unavailable_probes.setdefault(
            "execution-1",
            UnavailableExecutionProbe(execution, set()),
        )
        probe.account_names.add(account_name)
        return 0

    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.configured_linear_accounts",
        lambda: accounts,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_account_for_workspace",
        lambda _workspace: None,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_client",
        lambda *, account_name: _ClientContext(clients[account_name]),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.reconcile_provider_work_item_state",
        reconcile,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.retire_globally_unavailable_work_item",
        retire,
    )

    await reconcile_all_linear_work_items(
        {},
        {},
        review_plan=AsyncMock(),
        broadcast_host_message=AsyncMock(),
        defer_plan_review=AsyncMock(),
    )

    retire.assert_awaited_once_with(execution)


async def test_removed_workspace_issue_stays_pending_until_every_account_reports_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = (
        _Account("primary", _AccountConfig(public_source=True)),
        _Account("secondary", _AccountConfig(public_source=True)),
    )
    execution = _execution()
    clients = {account.name: object() for account in accounts}
    retire = AsyncMock()

    async def reconcile(
        _client: object,
        _boards: object,
        *,
        account_name: str,
        unavailable_probes: dict[str, UnavailableExecutionProbe],
    ) -> int:
        await sleep(0)
        if account_name == "primary":
            probe = unavailable_probes.setdefault(
                "execution-1",
                UnavailableExecutionProbe(execution, set()),
            )
            probe.account_names.add(account_name)
        return 0

    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.configured_linear_accounts",
        lambda: accounts,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_account_for_workspace",
        lambda _workspace: None,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_client",
        lambda *, account_name: _ClientContext(clients[account_name]),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.reconcile_provider_work_item_state",
        reconcile,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.retire_globally_unavailable_work_item",
        retire,
    )

    await reconcile_all_linear_work_items(
        {},
        {},
        review_plan=AsyncMock(),
        broadcast_host_message=AsyncMock(),
        defer_plan_review=AsyncMock(),
    )

    retire.assert_not_awaited()
