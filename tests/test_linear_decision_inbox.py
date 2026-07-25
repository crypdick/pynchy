"""Behavioral tests for host-leased Linear execution admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.config import PluginConfig
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_decision_inbox import (
    polling_boards,
    reconcile_linear_decision_inbox,
)
from pynchy.state import (
    WorkItemTransitionRequest,
    begin_work_item_transition,
    get_active_work_item_execution,
    get_all_tasks,
    get_work_item_transition_by_request,
    init_test_database,
    resolve_work_item_transition,
)
from pynchy.types import WorkItemExecutionStatus, WorkItemTransitionStatus


@dataclass(frozen=True)
class _Workspace:
    folder: str
    name: str
    jid: str


def _state(status: str) -> dict[str, str]:
    states = {
        "human_approved": {
            "id": "state-approved",
            "name": "Human Approved",
            "type": "unstarted",
        },
        "in_progress": {
            "id": "state-progress",
            "name": "In Progress",
            "type": "started",
        },
        "follow_ups": {
            "id": "state-follow-ups",
            "name": "Follow-ups",
            "type": "started",
        },
    }
    return states[status]


def _issue(
    issue_id: str,
    identifier: str,
    title: str,
    status: str,
    project_id: str,
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "identifier": identifier,
        "title": title,
        "url": f"https://linear.app/example/issue/{identifier}",
        "updatedAt": "2026-07-19T08:00:00+00:00",
        "state": _state(status),
        "project": {"id": project_id, "name": project_id},
    }


class _DecisionClient(LinearClient):
    team_key = "SYN"

    def __init__(self) -> None:
        self.issues_by_state = {
            "state-approved": [
                _issue(
                    "issue-execute",
                    "SYN-3",
                    "Execute an approved task",
                    "human_approved",
                    "project-beta",
                ),
                _issue(
                    "issue-unmanaged",
                    "SYN-4",
                    "Ignore an unrelated project",
                    "human_approved",
                    "project-unmanaged",
                ),
            ],
            "state-progress": [],
            "state-follow-ups": [],
        }

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        return next(
            (
                issue
                for issues in self.issues_by_state.values()
                for issue in issues
                if issue["id"] == issue_id
            ),
            None,
        )

    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        if "PynchyLinearDecisionInbox" in query:
            issues = self.issues_by_state[str(variables["state_id"])]
            return {
                "workflowState": {
                    "issues": {
                        "nodes": issues,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        issue = await self.get_issue(str(variables["issue_id"]))
        if issue is None:
            raise AssertionError("test update targeted an unknown issue")
        old_state_id = str(issue["state"]["id"])
        new_state_id = str(variables["state_id"])
        self.issues_by_state[old_state_id].remove(issue)
        issue["state"] = next(
            state
            for state in (
                _state("human_approved"),
                _state("in_progress"),
                _state("follow_ups"),
            )
            if state["id"] == new_state_id
        )
        self.issues_by_state[new_state_id].append(issue)
        return {"issueUpdate": {"success": True, "issue": issue}}


class _PagedDecisionClient(_DecisionClient):
    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        if "PynchyLinearDecisionInbox" not in query:
            return await super().query(query, **variables)
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


def _board(project_id: str) -> LinearWorkspaceBoard:
    return LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": project_id},
        states={
            "human_approved": _state("human_approved"),
            "in_progress": _state("in_progress"),
            "follow_ups": _state("follow_ups"),
        },
    )


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


async def test_reconcile_leases_authorized_work_before_admitting_one_task() -> None:
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
    assert len(created) == 1
    task = created[0]
    execution = await get_active_work_item_execution("issue-execute")
    assert execution is not None
    assert execution.status.value == "in_progress"
    assert execution.task_id == task.id
    assert execution.initiated_by == "linear-decision-inbox"
    assert task.group_folder == "beta"
    assert task.input_source == "external:linear:authorized"
    assert task.context_mode == "isolated"
    assert task.derived_thread_name == "[SYN-3] Execute an approved task"
    assert "Objective:" in task.prompt
    assert "Authority:" in task.prompt
    assert "Success:" in task.prompt
    assert "linear_claim_work_item" not in task.prompt
    assert len(await get_all_tasks()) == 1


async def test_private_account_keeps_authorized_context_trusted() -> None:
    created = await reconcile_linear_decision_inbox(
        _DecisionClient(),
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
        public_source=False,
    )

    task = created[0]
    assert task.input_source == "trusted:linear:authorized"
    assert "EXTERNAL_UNTRUSTED_CONTENT" not in task.prompt


async def test_reconcile_admits_follow_up_work_without_a_second_approval_lease() -> None:
    client = _DecisionClient()
    follow_up = _issue(
        "issue-follow-up",
        "SYN-5",
        "Finish operational cleanup",
        "follow_ups",
        "project-beta",
    )
    client.issues_by_state["state-follow-ups"].append(follow_up)

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
        public_source=False,
    )
    duplicate = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 6, tzinfo=UTC),
        public_source=False,
    )

    follow_up_tasks = [task for task in created if task.id.startswith("linear-follow-ups-")]
    assert len(follow_up_tasks) == 1
    assert follow_up_tasks[0].input_source == "trusted:linear:follow-ups"
    assert "preserve useful logs before teardown" in follow_up_tasks[0].prompt
    assert await get_active_work_item_execution("issue-follow-up") is None
    assert duplicate == []


async def test_reauthorized_handoff_creates_a_new_execution_task() -> None:
    client = _DecisionClient()
    workspaces = [_Workspace("beta", "Beta", "linear:beta")]
    boards = {"beta": _board("project-beta")}
    first = await reconcile_linear_decision_inbox(
        client,
        workspaces,
        boards,
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )
    execution = await get_active_work_item_execution("issue-execute")
    assert execution is not None
    transition = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="handoff-1",
            operation="record_handoff",
            target_status="blocked",
            result_execution_status=WorkItemExecutionStatus.HANDED_OFF,
        )
    )
    assert await get_work_item_transition_by_request("handoff-1") == transition
    await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.HANDED_OFF,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
    )
    issue = client.issues_by_state["state-progress"].pop()
    issue["state"] = _state("human_approved")
    issue["updatedAt"] = "2026-07-19T09:00:00+00:00"
    client.issues_by_state["state-approved"].append(issue)

    second = await reconcile_linear_decision_inbox(
        client,
        workspaces,
        boards,
        now=datetime(2026, 7, 19, 9, 5, tzinfo=UTC),
    )

    assert len(second) == 1
    assert second[0].id != first[0].id
    current = await get_active_work_item_execution("issue-execute")
    assert current is not None
    assert current.attempt == 2


async def test_reconcile_ignores_issues_without_a_project() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"].insert(
        0,
        {
            **_issue(
                "issue-no-project",
                "SYN-0",
                "Not assigned to a project",
                "human_approved",
                "unused",
            ),
            "project": None,
        },
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    assert [task.group_folder for task in created] == ["beta"]


async def test_reconcile_paginates_through_large_authorized_backlog() -> None:
    client = _PagedDecisionClient()
    client.issues_by_state["state-approved"] = [
        _issue(
            f"issue-{number}",
            f"SYN-{number}",
            f"Execute item {number}",
            "human_approved",
            "project-alpha",
        )
        for number in range(1, 62)
    ]

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
