"""Provider boundary validation for Linear work-item operations."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pynchy.config.api import LinearTool
from pynchy.plugins.integrations.linear_accounts import LinearAccount
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearClientContext,
    LinearWorkspaceIssueError,
    linear_client,
    state_id,
    workspace_issue,
)
from tests.linear_work_items_support import _board


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
