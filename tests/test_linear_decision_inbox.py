"""Behavioral tests for Linear decision-state task admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.config import PluginConfig
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_decision_inbox import (
    polling_boards,
    reconcile_linear_decision_inbox,
)
from pynchy.state import get_all_tasks, init_test_database


@dataclass(frozen=True)
class _Workspace:
    folder: str
    name: str
    jid: str


class _DecisionClient:
    def __init__(self) -> None:
        self.issues_by_state = {
            "state-ready": [
                _issue(
                    "issue-plan",
                    "SYN-1",
                    "Plan the task inbox",
                    "state-ready",
                    "project-alpha",
                ),
                _issue(
                    "issue-unmanaged",
                    "SYN-2",
                    "Ignore an unrelated project",
                    "state-ready",
                    "project-unmanaged",
                ),
            ],
            "state-approved": [
                _issue(
                    "issue-execute",
                    "SYN-3",
                    "Execute an approved task",
                    "state-approved",
                    "project-beta",
                )
            ],
        }

    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        assert "PynchyLinearDecisionInbox" in query
        issues = self.issues_by_state[str(variables["state_id"])]
        return {
            "workflowState": {
                "issues": {
                    "nodes": issues,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }


class _PagedDecisionClient(_DecisionClient):
    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        assert "PynchyLinearDecisionInbox" in query
        issues = self.issues_by_state[str(variables["state_id"])]
        start = int(str(variables["after"] or 0))
        end = start + 50
        has_next = end < len(issues)
        return {
            "workflowState": {
                "issues": {
                    "nodes": issues[start:end],
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": str(end) if has_next else None,
                    },
                }
            }
        }


def _issue(
    issue_id: str,
    identifier: str,
    title: str,
    state_id: str,
    project_id: str,
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "identifier": identifier,
        "title": title,
        "url": f"https://linear.app/example/issue/{identifier}",
        "updatedAt": "2026-07-19T08:00:00+00:00",
        "state": {"id": state_id},
        "project": {"id": project_id, "name": project_id},
    }


def _board(project_id: str) -> LinearWorkspaceBoard:
    return LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": project_id},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "human_approved": {"id": "state-approved"},
        },
    )


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


async def test_reconcile_admits_each_human_decision_to_its_own_workspace_once() -> None:
    client = _DecisionClient()
    workspaces = [
        _Workspace("alpha", "Alpha", "linear:alpha"),
        _Workspace("beta", "Beta", "linear:beta"),
    ]
    boards = {
        "alpha": _board("project-alpha"),
        "beta": _board("project-beta"),
    }
    now = datetime(2026, 7, 19, 8, 5, tzinfo=UTC)

    created = await reconcile_linear_decision_inbox(client, workspaces, boards, now=now)
    duplicate = await reconcile_linear_decision_inbox(client, workspaces, boards, now=now)

    assert duplicate == []
    assert {task.group_folder for task in created} == {"alpha", "beta"}
    assert {task.input_source for task in created} == {
        "external:linear:ready_for_planning",
        "external:linear:human_approved",
    }
    assert all(task.context_mode == "isolated" for task in created)
    assert {task.derived_thread_name for task in created} == {
        "[SYN-1] Plan the task inbox",
        "[SYN-3] Execute an approved task",
    }
    assert len(await get_all_tasks()) == 2


async def test_planning_task_requires_a_persisted_plan_without_execution_authority() -> None:
    created = await reconcile_linear_decision_inbox(
        _DecisionClient(),
        [_Workspace("alpha", "Alpha", "linear:alpha")],
        {"alpha": _board("project-alpha")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    task = created[0]
    assert "linear_submit_plan" in task.prompt
    assert "Awaiting Plan Approval" in task.prompt
    assert "Do not claim or execute" in task.prompt
    assert "EXTERNAL_UNTRUSTED_CONTENT" in task.prompt


async def test_private_account_decision_context_remains_trusted() -> None:
    created = await reconcile_linear_decision_inbox(
        _DecisionClient(),
        [_Workspace("alpha", "Alpha", "linear:alpha")],
        {"alpha": _board("project-alpha")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
        public_source=False,
    )

    task = created[0]
    assert task.input_source == "trusted:linear:ready_for_planning"
    assert "EXTERNAL_UNTRUSTED_CONTENT" not in task.prompt


async def test_reconcile_ignores_issues_without_a_project() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-ready"].insert(
        0,
        {
            **_issue(
                "issue-no-project",
                "SYN-0",
                "Not assigned to a project",
                "state-ready",
                "unused",
            ),
            "project": None,
        },
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("alpha", "Alpha", "linear:alpha")],
        {"alpha": _board("project-alpha")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    assert [task.group_folder for task in created] == ["alpha"]


async def test_approved_task_requires_claim_before_execution() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-ready"] = []

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    task = created[0]
    assert "linear_claim_work_item" in task.prompt
    assert "Human Approved" in task.prompt
    assert "linear_await_review_work_item" in task.prompt


async def test_reconcile_paginates_through_a_large_planning_backlog() -> None:
    client = _PagedDecisionClient()
    client.issues_by_state["state-ready"] = [
        _issue(
            f"issue-{number}",
            f"SYN-{number}",
            f"Plan item {number}",
            "state-ready",
            "project-alpha",
        )
        for number in range(1, 62)
    ]
    client.issues_by_state["state-approved"] = []

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("alpha", "Alpha", "linear:alpha")],
        {"alpha": _board("project-alpha")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    assert len(created) == 61


def test_webhook_routed_workspace_does_not_also_use_polling() -> None:
    settings = make_settings(
        plugins={
            "linear": PluginConfig(
                options={
                    "webhook_routes": [
                        {"name": "alpha", "workspace": "alpha"},
                    ]
                }
            )
        }
    )
    boards = {
        "alpha": _board("project-alpha"),
        "beta": _board("project-beta"),
    }

    with patch(
        "pynchy.plugins.integrations.linear_decision_inbox.get_settings",
        return_value=settings,
    ):
        fallback = polling_boards(boards)

    assert fallback == {"beta": boards["beta"]}


def test_project_routed_webhook_covers_every_managed_board() -> None:
    settings = make_settings(
        plugins={"linear": PluginConfig(options={"webhook_routes": [{"name": "all-boards"}]})}
    )
    boards = {
        "fam": _board("project-fam"),
        "pynchy-dev": _board("project-pynchy"),
    }

    with patch(
        "pynchy.plugins.integrations.linear_decision_inbox.get_settings",
        return_value=settings,
    ):
        fallback = polling_boards(boards)

    assert fallback == {}
