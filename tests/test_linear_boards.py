"""Tests for Linear workspace board reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pynchy.plugins.integrations.linear_boards import (
    LINEAR_TODO_STATUSES,
    LinearBoardError,
    WorkspaceTodoProposal,
    create_workspace_todo,
    list_workspace_todos,
    move_workspace_todo,
    provision_workspace_board,
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
        self.issues: list[dict[str, Any]] = []
        self.queries: list[str] = []

    async def list_teams(self) -> list[dict[str, Any]]:
        return self.teams

    async def query(self, query: str, **variables: Any) -> dict[str, Any]:
        self.queries.append(query)
        if "TeamLinearBoardResources" in query:
            page_start = int(variables["projects_after"] or 0)
            page_end = page_start + 50
            next_page_start = str(page_end) if page_end < len(self.projects) else None
            return {
                "team": {
                    "projects": {
                        "nodes": self.projects[page_start:page_end],
                        "pageInfo": {
                            "hasNextPage": next_page_start is not None,
                            "endCursor": next_page_start,
                        },
                    },
                    "states": {"nodes": self.states},
                }
            }
        if "CreateWorkspaceProject" in query:
            project = {
                "id": f"project-{len(self.projects) + 1}",
                "name": variables["name"],
                "url": f"https://linear.app/acme/project/{len(self.projects) + 1}",
                "description": variables["description"],
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
            state = next(state for state in self.states if state["id"] == variables["state_id"])
            issue = {
                "id": "issue-1",
                "identifier": "SYN-1",
                "title": variables["title"],
                "url": "https://linear.app/acme/issue/SYN-1",
                "state": state,
                "project": {"id": variables["project_id"], "name": "Code Improver"},
            }
            self.created_issues.append(variables)
            self.issues.append(issue)
            return {"issueCreate": {"success": True, "issue": issue}}
        if "ListWorkspaceTodos" in query:
            response = {"project": {"issues": {"nodes": self.issues}}}
        elif "MoveWorkspaceTodo" in query:
            issue = {
                "id": variables["issue_id"],
                "identifier": variables["issue_id"],
                "title": "Ship Linear todos",
                "url": "https://linear.app/acme/issue/SYN-1",
                "state": {"id": variables["state_id"], "name": "In Progress"},
            }
            self.updated_issues.append(variables)
            response = {"issueUpdate": {"success": True, "issue": issue}}
        else:
            message = f"Unexpected query: {query}"
            raise AssertionError(message)
        return response


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

        board = await provision_workspace_board(client, workspace, team_key=None)

        assert board.project["name"] == "Code Improver"
        assert board.states.keys() == LINEAR_TODO_STATUSES.keys()
        created_state_names = {state["name"] for state in client.states}
        assert {
            "Agent Proposed",
            "Human Approved",
            "In Progress",
            "Awaiting Review",
            "Follow-ups",
            "Blocked",
            "Done",
            "Rejected",
        } <= created_state_names

    async def test_adds_awaiting_review_to_an_existing_workspace_board(self):
        client = FakeLinearClient()
        client.states.extend(
            {
                "id": f"state-{key.replace('_', '-')}",
                "name": spec.name,
                "type": spec.type,
                "position": spec.position,
            }
            for key, spec in LINEAR_TODO_STATUSES.items()
            if key != "awaiting_review"
        )
        client.projects.append(
            {
                "id": "project-existing",
                "name": "Code Improver",
                "url": "https://linear.app/acme/project/existing",
            }
        )
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")

        board = await provision_workspace_board(client, workspace, team_key=None)

        assert board.project["id"] == "project-existing"
        assert board.states["awaiting_review"]["name"] == "Awaiting Review"
        created = [query for query in client.queries if "CreateWorkflowState" in query]
        assert len(created) == 1

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

        board = await provision_workspace_board(client, workspace, team_key=None)

        assert board.project["id"] == "project-existing"
        assert len(client.projects) == 1

    async def test_reuses_existing_project_from_a_later_page(self):
        client = FakeLinearClient()
        client.projects.extend(
            {
                "id": f"project-{index}",
                "name": f"Unrelated {index}",
                "url": f"https://linear.app/acme/project/{index}",
            }
            for index in range(50)
        )
        client.projects.append(
            {
                "id": "project-existing",
                "name": "Code Improver",
                "url": "https://linear.app/acme/project/existing",
                "description": "Managed by Pynchy.\n\npynchy.workspace=code-improver\n",
            }
        )
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")

        board = await provision_workspace_board(client, workspace, team_key=None)

        assert board.project["id"] == "project-existing"
        assert len(client.projects) == 51

    async def test_project_name_uses_workspace_display_name(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="Custom Display Name")

        board = await provision_workspace_board(client, workspace, team_key=None)

        assert board.project["name"] == "Custom Display Name"

    async def test_project_name_uses_folder_when_display_name_is_repo_slug(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="crypdick--pynchy")

        board = await provision_workspace_board(client, workspace, team_key=None)

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

        board = await provision_workspace_board(client, workspace, team_key=None)

        assert board.project["id"] == "project-existing"
        assert board.project["name"] == "DDDD Evening Review"
        assert client.updated_projects[0]["name"] == "DDDD Evening Review"

    async def test_rejects_duplicate_workspace_projects_without_mutating_them(self):
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

        with pytest.raises(LinearBoardError, match="Duplicate Linear projects"):
            await provision_workspace_board(client, workspace, team_key=None)

        assert client.updated_projects == []

    async def test_workspace_marker_does_not_match_a_longer_workspace_name(self):
        client = FakeLinearClient()
        client.projects.extend(
            [
                {
                    "id": "project-general",
                    "name": "General",
                    "url": "https://linear.app/acme/project/general",
                    "description": "Managed by Pynchy.\n\npynchy.workspace=general\n",
                },
                {
                    "id": "project-general-voice",
                    "name": "General Voice",
                    "url": "https://linear.app/acme/project/general-voice",
                    "description": "Managed by Pynchy.\n\npynchy.workspace=general-voice\n",
                },
            ]
        )
        workspace = WorkspaceStub(folder="general", name="General")

        board = await provision_workspace_board(client, workspace, team_key=None)

        assert board.project["id"] == "project-general"
        assert client.updated_projects == []

    async def test_adopts_existing_project_by_name_with_workspace_marker(self):
        client = FakeLinearClient()
        client.projects.append(
            {
                "id": "project-existing",
                "name": "Code Improver",
                "url": "https://linear.app/acme/project/existing",
            }
        )
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")

        board = await provision_workspace_board(client, workspace, team_key=None)

        assert board.project["id"] == "project-existing"
        assert "pynchy.workspace=code-improver" in board.project["description"]


class TestWorkspaceTodos:
    async def test_listing_missing_board_fails_without_creating_provider_resources(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")

        with pytest.raises(LinearBoardError, match="has not been provisioned"):
            await list_workspace_todos(client, workspace, team_key=None)

        assert client.projects == []
        assert not any("CreateWorkspaceProject" in query for query in client.queries)
        assert not any("CreateWorkflowState" in query for query in client.queries)

    async def test_todo_creation_missing_board_fails_without_creating_project(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")

        with pytest.raises(LinearBoardError, match="has not been provisioned"):
            await create_workspace_todo(
                client,
                workspace,
                WorkspaceTodoProposal(title="Review docs"),
                team_key=None,
            )

        assert client.projects == []
        assert client.created_issues == []
        assert not any("CreateWorkspaceProject" in query for query in client.queries)

    async def test_agent_created_workspace_todo_preserves_proposal_details(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")
        await provision_workspace_board(client, workspace, team_key=None)

        issue = await create_workspace_todo(
            client,
            workspace,
            WorkspaceTodoProposal(
                title="Review docs",
                description=(
                    "Evidence: docs/integrations/linear.md\n\nAcceptance: clarify the workflow."
                ),
                priority=4,
            ),
            team_key=None,
        )

        assert issue["identifier"] == "SYN-1"
        assert issue["state"]["name"] == "Agent Proposed"
        assert client.created_issues[0]["project_id"] == "project-1"
        assert client.created_issues[0]["state_id"] == "state-agent-proposed"
        description = client.created_issues[0]["description"]
        assert description.startswith("Evidence: docs/integrations/linear.md")
        assert "pynchy.workspace=code-improver" in description
        assert client.created_issues[0]["priority"] == 4

    async def test_open_todo_listing_excludes_done_and_rejected_items(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")
        board = await provision_workspace_board(client, workspace, team_key=None)
        client.issues = [
            {"id": "open", "state": board.states["agent_proposed"]},
            {"id": "done", "state": board.states["done"]},
            {"id": "rejected", "state": board.states["rejected"]},
        ]

        issues = await list_workspace_todos(client, workspace, team_key=None)

        assert [issue["id"] for issue in issues] == ["open"]
        assert "title description url" in client.queries[-1]

    async def test_move_workspace_todo_maps_status_to_linear_state(self):
        client = FakeLinearClient()
        workspace = WorkspaceStub(folder="code-improver", name="Code Improver")
        await provision_workspace_board(client, workspace, team_key=None)

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
