"""Tests for Linear workspace board reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pynchy.plugins.integrations.linear_boards import (
    LINEAR_TODO_STATUSES,
    LinearBoardError,
    create_workspace_todo,
    ensure_workspace_board,
    move_workspace_todo,
    select_team,
)


@dataclass
class WorkspaceStub:
    folder: str
    name: str
    jid: str = "slack:C123"


class FakeLinearClient:
    def __init__(self) -> None:
        self.teams = [{"id": "team-1", "key": "SYN", "name": "Synapse"}]
        self.projects: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = [
            {"id": "state-backlog", "name": "Backlog", "type": "backlog", "position": 10.0},
        ]
        self.updated_projects: list[dict[str, Any]] = []
        self.created_issues: list[dict[str, Any]] = []
        self.updated_issues: list[dict[str, Any]] = []

    async def list_teams(self) -> list[dict[str, Any]]:
        return self.teams

    async def query(self, query: str, **variables: Any) -> dict[str, Any]:
        if "TeamLinearBoardResources" in query:
            return {
                "team": {
                    "projects": {"nodes": self.projects},
                    "states": {"nodes": self.states},
                }
            }
        if "CreateWorkspaceProject" in query:
            project = {
                "id": f"project-{len(self.projects) + 1}",
                "name": variables["name"],
                "url": f"https://linear.app/acme/project/{len(self.projects) + 1}",
            }
            self.projects.append(project)
            return {"projectCreate": {"success": True, "project": project}}
        if "UpdateWorkspaceProject" in query:
            project = next(
                project for project in self.projects if project["id"] == variables["project_id"]
            )
            project["name"] = variables["name"]
            project["description"] = variables["description"]
            self.updated_projects.append(variables)
            return {"projectUpdate": {"success": True, "project": project}}
        if "CreateWorkflowState" in query:
            state = {
                "id": f"state-{variables['name'].lower().replace(' ', '-')}",
                "name": variables["name"],
                "type": variables["type"],
                "position": variables["position"],
            }
            self.states.append(state)
            return {"workflowStateCreate": {"success": True, "workflowState": state}}
        if "CreateWorkspaceTodo" in query:
            issue = {
                "id": "issue-1",
                "identifier": "SYN-1",
                "title": variables["title"],
                "url": "https://linear.app/acme/issue/SYN-1",
                "state": {"id": variables["state_id"], "name": "Backlog", "type": "backlog"},
                "project": {"id": variables["project_id"], "name": "Code Improver"},
            }
            self.created_issues.append(variables)
            return {"issueCreate": {"success": True, "issue": issue}}
        if "MoveWorkspaceTodo" in query:
            issue = {
                "id": variables["issue_id"],
                "identifier": variables["issue_id"],
                "title": "Ship Linear todos",
                "url": "https://linear.app/acme/issue/SYN-1",
                "state": {"id": variables["state_id"], "name": "In Progress"},
            }
            self.updated_issues.append(variables)
            return {"issueUpdate": {"success": True, "issue": issue}}
        message = f"Unexpected query: {query}"
        raise AssertionError(message)


class TestSelectTeam:
    async def test_uses_only_visible_team_without_team_key(self):
        client = FakeLinearClient()

        team = await select_team(client, team_key=None)

        assert team["id"] == "team-1"

    async def test_requires_team_key_when_multiple_teams_are_visible(self):
        client = FakeLinearClient()
        client.teams.append({"id": "team-2", "key": "OPS", "name": "Ops"})

        with pytest.raises(LinearBoardError, match="LINEAR_TEAM_KEY"):
            await select_team(client, team_key=None)

    async def test_team_key_selects_matching_team(self):
        client = FakeLinearClient()
        client.teams.append({"id": "team-2", "key": "OPS", "name": "Ops"})

        team = await select_team(client, team_key="OPS")

        assert team["id"] == "team-2"

    async def test_team_key_can_match_team_id(self):
        client = FakeLinearClient()
        client.teams.append({"id": "team-2", "key": "OPS", "name": "Ops"})

        team = await select_team(client, team_key="team-2")

        assert team["key"] == "OPS"

    async def test_team_key_can_match_team_name_case_insensitively(self):
        client = FakeLinearClient()
        client.teams.append({"id": "team-2", "key": "OPS", "name": "Ops"})

        team = await select_team(client, team_key="ops")

        assert team["id"] == "team-2"

    async def test_rejects_malformed_team_payloads_at_the_boundary(self):
        client = FakeLinearClient()
        client.teams = [{"key": "OPS", "name": "Ops"}]

        with pytest.raises(LinearBoardError, match="missing string id"):
            await select_team(client, team_key=None)


class TestEnsureWorkspaceBoard:
    async def test_creates_missing_project_and_workflow_states(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")

        board = await ensure_workspace_board(client, workspace, team_key=None)

        assert board.project["name"] == "Code Improver"
        assert board.states.keys() == LINEAR_TODO_STATUSES.keys()
        created_state_names = {state["name"] for state in client.states}
        assert {"Backlog", "Planning", "Ready", "In Progress", "Done"} <= created_state_names

    async def test_reuses_existing_project_by_name(self):
        client = FakeLinearClient()
        client.projects.append(
            {
                "id": "project-existing",
                "name": "Code Improver",
                "url": "https://linear.app/acme/project/existing",
            }
        )
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")

        board = await ensure_workspace_board(client, workspace, team_key=None)

        assert board.project["id"] == "project-existing"
        assert len(client.projects) == 1

    async def test_project_name_uses_workspace_display_name(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="Custom Display Name")

        board = await ensure_workspace_board(client, workspace, team_key=None)

        assert board.project["name"] == "Custom Display Name"

    async def test_project_name_uses_folder_when_display_name_is_repo_slug(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="crypdick--pynchy")

        board = await ensure_workspace_board(client, workspace, team_key=None)

        assert board.project["name"] == "Code Improver"

    async def test_renames_existing_prefixed_project_by_workspace_metadata(self):
        client = FakeLinearClient()
        client.projects.append(
            {
                "id": "project-existing",
                "name": "Pynchy: Topic 6011",
                "url": "https://linear.app/acme/project/existing",
                "description": "Managed by Pynchy.\n\npynchy.workspace=topic-6011\n",
            }
        )
        workspace = WorkspaceStub(folder="topic-6011", name="DDDD Evening Review")

        board = await ensure_workspace_board(client, workspace, team_key=None)

        assert board.project["id"] == "project-existing"
        assert board.project["name"] == "DDDD Evening Review"
        assert client.updated_projects[0]["name"] == "DDDD Evening Review"

    async def test_renames_all_stale_workspace_projects_by_metadata(self):
        client = FakeLinearClient()
        client.projects.extend(
            [
                {
                    "id": "project-current",
                    "name": "crypdick--pynchy",
                    "url": "https://linear.app/acme/project/current",
                    "description": "Managed by Pynchy.\n\npynchy.workspace=code-improver\n",
                },
                {
                    "id": "project-prefixed",
                    "name": "Pynchy: Code Improver",
                    "url": "https://linear.app/acme/project/prefixed",
                    "description": "Managed by Pynchy.\n\npynchy.workspace=code-improver\n",
                },
            ]
        )
        workspace = WorkspaceStub(folder="code-improver", name="crypdick--pynchy")

        board = await ensure_workspace_board(client, workspace, team_key=None)

        assert board.project["name"] == "Code Improver"
        assert {project["name"] for project in client.projects} == {"Code Improver"}
        assert {project["project_id"] for project in client.updated_projects} == {
            "project-current",
            "project-prefixed",
        }


class TestWorkspaceTodos:
    async def test_create_workspace_todo_uses_backlog_state_and_workspace_project(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")

        issue = await create_workspace_todo(client, workspace, "Review docs", team_key=None)

        assert issue["identifier"] == "SYN-1"
        assert client.created_issues[0]["project_id"] == "project-1"
        assert client.created_issues[0]["state_id"] == "state-backlog"
        assert "pynchy.workspace=code-improver" in client.created_issues[0]["description"]

    async def test_move_workspace_todo_maps_status_to_linear_state(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")
        await ensure_workspace_board(client, workspace, team_key=None)

        issue = await move_workspace_todo(
            client,
            workspace,
            issue_id="SYN-1",
            status="in_progress",
            team_key=None,
        )

        assert issue["state"]["name"] == "In Progress"
        assert client.updated_issues[0]["issue_id"] == "SYN-1"
        assert client.updated_issues[0]["state_id"] == "state-in-progress"
