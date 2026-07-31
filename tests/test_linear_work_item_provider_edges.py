"""Provider boundary validation for Linear work-item operations."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from pynchy.config.api import LinearTool
from pynchy.plugins.integrations.linear_accounts import LinearAccount
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearClientContext,
    LinearWorkItemRuntime,
    LinearWorkspaceIssueError,
    WorkItemLeaseRequest,
    acquire_work_item_lease,
    configure_linear_work_item_runtime,
    linear_client,
    state_id,
    workspace_issue,
)
from pynchy.work_items.api import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransition,
    WorkItemTransitionStatus,
)
from tests.linear_work_items_support import _board, _issue

if TYPE_CHECKING:
    from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard


class _Client(LinearClient):
    team_key = "SYN"

    def __init__(self, issue: dict[str, object] | None) -> None:
        self.issue = issue

    async def get_issue(self, _issue_id: str) -> dict[str, object] | None:
        return self.issue

    async def query(self, _query: str, **_variables: object) -> dict[str, object]:
        raise AssertionError("provider boundary tests do not issue GraphQL queries")

    async def create_comment(self, _issue_id: str, _body: str) -> dict[str, object]:
        raise AssertionError("provider boundary tests do not create comments")


def _runtime(
    *,
    get_transition_by_request: AsyncMock | None = None,
    get_execution: AsyncMock | None = None,
    get_active_execution: AsyncMock | None = None,
    create_claim: AsyncMock | None = None,
    resolve_transition: AsyncMock | None = None,
) -> LinearWorkItemRuntime:
    return LinearWorkItemRuntime(
        get_transition_by_request=get_transition_by_request or AsyncMock(return_value=None),
        get_execution=get_execution or AsyncMock(),
        get_active_execution=get_active_execution or AsyncMock(return_value=None),
        create_claim=create_claim or AsyncMock(),
        claim_request=AsyncMock(),
        begin_transition=AsyncMock(),
        transition_resolution=Mock(),
        resolve_transition=resolve_transition or AsyncMock(),
        resolve_transition_if_lifecycle_current=AsyncMock(),
    )


def _request(*, board: LinearWorkspaceBoard | None = None) -> WorkItemLeaseRequest:
    return WorkItemLeaseRequest(
        workspace="pynchy",
        issue_id="issue-1",
        request_id="lease-1",
        initiated_by="test",
        board=board,
    )


def _execution() -> WorkItemExecution:
    return WorkItemExecution(
        id="execution-1",
        workspace="pynchy",
        linear_issue_id="issue-1",
        linear_issue_identifier="PYN-1",
        linear_issue_url="https://linear.app/example/issue/PYN-1",
        turn_id=None,
        task_id=None,
        attempt=1,
        flow_id=None,
        temporal_workflow_id=None,
        initiated_by="test",
        observed_state_id="state-human-approved",
        observed_state_name="Human Approved",
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


def _transition() -> WorkItemTransition:
    return WorkItemTransition(
        id=1,
        execution_id="execution-1",
        request_id="lease-1",
        operation="claim",
        target_status="in_progress",
        result_execution_status=WorkItemExecutionStatus.IN_PROGRESS,
        evidence_refs=(),
        summary=None,
        blocker=None,
        handoff_to=None,
        status=WorkItemTransitionStatus.SUCCEEDED,
        receipt=None,
        error=None,
        created_at="2026-07-31T00:00:00+00:00",
        resolved_at=None,
    )


def test_linear_client_requires_exactly_one_account_selector() -> None:
    with pytest.raises(ValueError, match="exactly one workspace or account name"):
        linear_client()
    with pytest.raises(ValueError, match="exactly one workspace or account name"):
        linear_client(workspace="project", account_name="account")


def test_linear_client_rejects_a_workspace_without_a_selected_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider.linear_account_for_workspace",
        lambda _workspace: None,
    )

    with pytest.raises(ValueError, match="does not select a Linear account"):
        linear_client(workspace="project")


def test_linear_client_can_select_a_named_account(monkeypatch: pytest.MonkeyPatch) -> None:
    account = LinearAccount("named", LinearTool(type="linear"))
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider.linear_account",
        lambda name: account if name == "named" else None,
    )

    assert isinstance(linear_client(account_name="named"), LinearClientContext)


async def test_linear_client_context_requires_the_account_api_key() -> None:
    account = LinearAccount("named", LinearTool(type="linear"))

    with pytest.raises(ValueError, match="LINEAR_API_KEY is not configured"):
        async with LinearClientContext(account):
            pass


async def test_linear_work_item_lease_requires_configured_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider._runtime.runtime", None
    )

    with pytest.raises(RuntimeError, match="runtime has not been configured"):
        await acquire_work_item_lease(_Client(_issue()), _request())


@pytest.mark.parametrize(
    ("issue", "message"),
    [
        (None, "does not exist"),
        (
            {**_issue(), "project": {"id": "other"}},
            "does not belong to this Pynchy workspace board",
        ),
    ],
)
async def test_lease_rejects_missing_or_foreign_board_issues(
    issue: dict[str, object] | None,
    message: str,
) -> None:
    configure_linear_work_item_runtime(_runtime())

    with pytest.raises(LinearWorkspaceIssueError, match=message):
        await acquire_work_item_lease(_Client(issue), _request(board=_board()))


async def test_interrupted_succeeded_lease_is_idempotent() -> None:
    execution = _execution()
    transition = _transition()
    configure_linear_work_item_runtime(
        _runtime(
            get_transition_by_request=AsyncMock(return_value=transition),
            get_execution=AsyncMock(return_value=execution),
        )
    )

    assert await acquire_work_item_lease(_Client(None), _request()) == execution


@pytest.mark.parametrize(
    ("issue", "message"),
    [
        (None, "does not exist"),
        ({"project": {"id": "other"}}, "does not belong to this Pynchy workspace board"),
    ],
)
async def test_workspace_issue_enforces_provider_existence_and_project_ownership(
    monkeypatch: pytest.MonkeyPatch,
    issue: dict[str, object] | None,
    message: str,
) -> None:
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider.require_workspace_board",
        AsyncMock(return_value=_board()),
    )

    with pytest.raises(LinearWorkspaceIssueError, match=message):
        await workspace_issue(_Client(issue), "pynchy", "issue-1")


@pytest.mark.parametrize(
    ("payload", "error", "message"),
    [
        ({"state": None}, TypeError, "missing state"),
        ({"state": {}}, ValueError, "missing id"),
        ({"state": {"id": 42}}, ValueError, "missing id"),
    ],
)
def test_state_id_rejects_malformed_state_payload(
    payload: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        state_id(payload)


def test_state_id_accepts_a_workflow_state_payload() -> None:
    assert state_id({"id": "state-1"}) == "state-1"
    assert state_id({"state": {"id": "state-2"}}) == "state-2"
