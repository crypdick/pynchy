"""Behavioral tests for host-leased Linear execution admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.state import (
    init_test_database,
)


@dataclass(frozen=True)
class _Workspace:
    folder: str
    name: str
    jid: str


@dataclass(frozen=True)
class _LinearAccount:
    name: str


@dataclass(frozen=True)
class _ControlBinding:
    thread_jid: str
    closed: bool


def _state(status: str) -> dict[str, str]:
    states = {
        "ready_for_planning": {
            "id": "state-ready",
            "name": "Ready for Planning",
            "type": "unstarted",
        },
        "awaiting_plan_approval": {
            "id": "state-awaiting-plan",
            "name": "Awaiting Plan Approval",
            "type": "unstarted",
        },
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
    description: str = "",
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "identifier": identifier,
        "title": title,
        "description": description,
        "url": f"https://linear.app/example/issue/{identifier}",
        "updatedAt": "2026-07-19T08:00:00+00:00",
        "state": _state(status),
        "project": {"id": project_id, "name": project_id},
    }


class _DecisionClient(LinearClient):
    team_key = "SYN"

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.issues_by_state = {
            "state-ready": [],
            "state-awaiting-plan": [],
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
        if "CreateComment" in query:
            issue_id = str(variables["issue_id"])
            body = str(variables["body"])
            self.comments.append((issue_id, body))
            return {
                "commentCreate": {
                    "success": True,
                    "comment": {
                        "id": f"comment-{len(self.comments)}",
                        "body": body,
                        "createdAt": "2026-07-19T08:01:00+00:00",
                        "updatedAt": "2026-07-19T08:01:00+00:00",
                        "issue": {"id": issue_id},
                    },
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
                _state("awaiting_plan_approval"),
                _state("human_approved"),
                _state("in_progress"),
                _state("follow_ups"),
            )
            if state["id"] == new_state_id
        )
        if "description" in variables:
            issue["description"] = str(variables["description"])
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
            "ready_for_planning": _state("ready_for_planning"),
            "awaiting_plan_approval": _state("awaiting_plan_approval"),
            "human_approved": _state("human_approved"),
            "in_progress": _state("in_progress"),
            "follow_ups": _state("follow_ups"),
        },
    )


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()
