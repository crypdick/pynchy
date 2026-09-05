"""Linear workspace todo-board helpers.

Pynchy treats each workspace as the stable owner of a todo board. Linear does
not expose "boards" as a separate API object, so a workspace board is a Linear
Project plus shared team workflow states.
"""

from __future__ import annotations

from collections.abc import (
    Iterable,  # noqa: TC003 - beartype resolves this runtime annotation.
)
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_board_mutations import (
    apply_workspace_todo_move,
)
from pynchy.plugins.integrations.linear_board_payloads import (
    nodes,
    norm_name,
    normalize_status,
    payload_entity,
    projects_for_workspace,
)
from pynchy.plugins.integrations.linear_board_queries import (
    CREATE_WORKSPACE_TODO_MUTATION,
)
from pynchy.plugins.integrations.linear_board_resources import (
    load_team_resources,
    reconcile_workflow_state_position,
)
from pynchy.plugins.integrations.linear_board_selection import (
    require_todo_states,
    require_workspace_project,
)
from pynchy.plugins.integrations.linear_statuses import (
    AGENT_PROPOSED_STATUS,
    LINEAR_TODO_STATUSES,
    TERMINAL_STATE_TYPES,
)
from pynchy.plugins.integrations.linear_workspace_names import (
    project_description,
    todo_description,
    workspace_project_name,
)

_TEAM_PAYLOAD_NOT_OBJECT = "Linear team payload was not an object"
_TEAM_PAYLOAD_MISSING_ID = "Linear team payload missing string id"
_TEAM_KEY_NOT_VISIBLE = "LINEAR_TEAM_KEY did not match a visible Linear team: {team_key}"
_NO_VISIBLE_TEAMS = "Linear API key cannot see any teams"
_LINEAR_PROJECT_MISSING = "Linear response did not include project"
_LINEAR_ISSUES_PAGE_SIZE = 50


@runtime_checkable
class LinearQueryClient(Protocol):
    async def list_teams(self) -> list[dict[str, Any]]:
        """Return Linear teams visible to the configured credential."""

    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        """Run a Linear GraphQL query or mutation."""


@runtime_checkable
class WorkspaceLike(Protocol):
    @property
    def folder(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def jid(self) -> str: ...


@dataclass(frozen=True)
class LinearWorkspaceBoard:
    team: dict[str, Any]
    project: dict[str, Any]
    states: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class WorkspaceTodoProposal:
    title: str
    description: str | None = None
    priority: int | None = None


@dataclass(frozen=True)
class _VisibleLinearTeam:
    raw: dict[str, Any]
    team_id: str
    key: str | None
    name: str | None

    @classmethod
    def from_payload(cls, payload: object) -> _VisibleLinearTeam:
        if not isinstance(payload, dict):
            raise LinearBoardError(_TEAM_PAYLOAD_NOT_OBJECT)

        team_id = payload.get("id")
        if not isinstance(team_id, str) or not team_id:
            raise LinearBoardError(_TEAM_PAYLOAD_MISSING_ID)

        key = payload.get("key")
        name = payload.get("name")
        return cls(
            raw=payload,
            team_id=team_id,
            key=key if isinstance(key, str) and key else None,
            name=name if isinstance(name, str) and name else None,
        )

    def matches(self, team_key: str) -> bool:
        normalized = team_key.lower()
        return any(
            candidate.lower() == normalized
            for candidate in (self.team_id, self.key, self.name)
            if candidate is not None
        )

    @property
    def choice_label(self) -> str:
        return self.key or self.name or self.team_id


def _visible_teams(raw_teams: Iterable[object]) -> list[_VisibleLinearTeam]:
    return [_VisibleLinearTeam.from_payload(team) for team in raw_teams]


def _matching_team(teams: list[_VisibleLinearTeam], team_key: str) -> _VisibleLinearTeam | None:
    return next((team for team in teams if team.matches(team_key)), None)


def _visible_team_choices(teams: list[_VisibleLinearTeam]) -> str:
    return ", ".join(team.choice_label for team in teams)


async def select_team(
    client: LinearQueryClient,
    *,
    team_key: str | None,
) -> dict[str, Any]:
    """Select the Linear team to use, defaulting only when unambiguous."""
    teams = _visible_teams(await client.list_teams())
    if team_key:
        team = _matching_team(teams, team_key)
        if team is not None:
            return team.raw
        raise LinearBoardError(_TEAM_KEY_NOT_VISIBLE.format(team_key=team_key))

    if len(teams) == 1:
        return teams[0].raw
    if not teams:
        raise LinearBoardError(_NO_VISIBLE_TEAMS)
    raise LinearBoardError(
        "Multiple Linear teams are visible; set LINEAR_TEAM_KEY to one of: "
        + _visible_team_choices(teams)
    )


async def require_workspace_board(
    client: LinearQueryClient,
    workspace: WorkspaceLike,
    *,
    team_key: str | None,
) -> LinearWorkspaceBoard:
    """Load one pre-provisioned workspace board without mutating Linear."""
    team = await select_team(client, team_key=team_key)
    resources = await load_team_resources(client, str(team["id"]))
    states = require_todo_states(resources["states"], workspace.folder)
    project = require_workspace_project(resources["projects"], workspace)
    return LinearWorkspaceBoard(team=team, project=project, states=states)


async def reconcile_workspace_boards(
    client: LinearQueryClient,
    workspaces: Iterable[WorkspaceLike],
    *,
    team_key: str | None,
) -> dict[str, LinearWorkspaceBoard]:
    """Ensure all currently registered workspaces have Linear boards."""
    workspaces = list(workspaces)
    if not workspaces:
        return {}

    team = await select_team(client, team_key=team_key)
    resources = await load_team_resources(client, str(team["id"]))
    states = await _ensure_states(client, str(team["id"]), resources["states"])
    projects = resources["projects"]

    boards: dict[str, LinearWorkspaceBoard] = {}
    for workspace in workspaces:
        project = await _ensure_project(client, str(team["id"]), workspace, projects)
        if project not in projects:
            projects.append(project)
        boards[workspace.folder] = LinearWorkspaceBoard(team=team, project=project, states=states)
    return boards


async def create_workspace_todo(
    client: LinearQueryClient,
    workspace: WorkspaceLike,
    proposal: WorkspaceTodoProposal,
    *,
    team_key: str | None,
    status: str = AGENT_PROPOSED_STATUS,
) -> dict[str, Any]:
    """Create a Linear issue in the workspace's board."""
    return await _create_workspace_todo(
        client,
        workspace,
        replace(
            proposal,
            description=todo_description(workspace, proposal.description),
        ),
        team_key=team_key,
        status=status,
    )


async def _create_workspace_todo(
    client: LinearQueryClient,
    workspace: WorkspaceLike,
    proposal: WorkspaceTodoProposal,
    *,
    team_key: str | None,
    status: str,
) -> dict[str, Any]:
    board = await require_workspace_board(client, workspace, team_key=team_key)
    status_key = normalize_status(status)
    state = board.states[status_key]
    data = await client.query(
        CREATE_WORKSPACE_TODO_MUTATION,
        team_id=board.team["id"],
        project_id=board.project["id"],
        state_id=state["id"],
        title=proposal.title,
        description=proposal.description,
        priority=proposal.priority,
    )
    return payload_entity(data, "issueCreate", "issue")


async def move_workspace_todo(
    client: LinearQueryClient,
    workspace: WorkspaceLike,
    *,
    issue_id: str,
    status: str,
    team_key: str | None,
) -> dict[str, Any]:
    """Move a Linear todo issue to one of Pynchy's standard statuses."""
    board = await require_workspace_board(client, workspace, team_key=team_key)
    status_key = normalize_status(status)
    state = board.states[status_key]
    state_id = str(state["id"])
    return await apply_workspace_todo_move(client, issue_id=issue_id, state_id=state_id)


async def list_workspace_todos(
    client: LinearQueryClient,
    workspace: WorkspaceLike,
    *,
    team_key: str | None,
    include_done: bool = False,
) -> list[dict[str, Any]]:
    """List issues in the workspace's Linear project."""
    board = await require_workspace_board(client, workspace, team_key=team_key)
    issues: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        data = await client.query(
            """
            query ListWorkspaceTodos(
              $project_id: String!,
              $first: Int!,
              $after: String
            ) {
              project(id: $project_id) {
                issues(first: $first, after: $after) {
                  nodes {
                    id identifier title description url priority createdAt updatedAt
                    state { id name type }
                    project { id name }
                  }
                  pageInfo { hasNextPage endCursor }
                }
              }
            }
            """,
            project_id=board.project["id"],
            first=_LINEAR_ISSUES_PAGE_SIZE,
            after=after,
        )
        project = data.get("project")
        if not isinstance(project, dict):
            raise LinearBoardError(_LINEAR_PROJECT_MISSING)
        issues.extend(nodes(project, "issues"))
        connection = project["issues"]
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict):
            raise LinearBoardError("Linear issue response did not include pageInfo")
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not isinstance(after, str) or not after:
            raise LinearBoardError("Linear issue response did not include a pagination cursor")
    if include_done:
        return issues
    return [
        issue
        for issue in issues
        if (issue.get("state") or {}).get("type") not in TERMINAL_STATE_TYPES
    ]


async def _ensure_states(
    client: LinearQueryClient,
    team_id: str,
    existing_states: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_name = {norm_name(state.get("name")): state for state in existing_states}
    states: dict[str, dict[str, Any]] = {}
    for key, spec in LINEAR_TODO_STATUSES.items():
        existing = by_name.get(norm_name(spec.name))
        if existing is not None:
            states[key] = await reconcile_workflow_state_position(
                client,
                existing,
                position=spec.position,
            )
            continue
        data = await client.query(
            """
            mutation CreateWorkflowState(
              $team_id: String!,
              $name: String!,
              $type: String!,
              $position: Float!,
              $color: String!
            ) {
              workflowStateCreate(input: {
                teamId: $team_id,
                name: $name,
                type: $type,
                position: $position,
                color: $color
              }) {
                success
                workflowState { id name type position }
              }
            }
            """,
            team_id=team_id,
            name=spec.name,
            type=spec.type,
            position=spec.position,
            color=spec.color,
        )
        states[key] = payload_entity(data, "workflowStateCreate", "workflowState")
    return states


async def _ensure_project(
    client: LinearQueryClient,
    team_id: str,
    workspace: WorkspaceLike,
    existing_projects: list[dict[str, Any]],
) -> dict[str, Any]:
    project_name = workspace_project_name(workspace)
    workspace_projects = projects_for_workspace(existing_projects, workspace)
    if len(workspace_projects) > 1:
        raise _duplicate_projects_error(workspace_projects, workspace)
    if workspace_projects:
        existing = workspace_projects[0]
        if norm_name(existing.get("name")) == norm_name(project_name) and existing.get(
            "description"
        ) == project_description(workspace):
            return existing
        updated = await _update_project(client, existing, workspace)
        existing.update(updated)
        return existing

    named_projects = [
        project
        for project in existing_projects
        if norm_name(project.get("name")) == norm_name(project_name)
    ]
    if len(named_projects) > 1:
        raise _duplicate_projects_error(named_projects, workspace)
    if named_projects:
        existing = named_projects[0]
        updated = await _update_project(client, existing, workspace)
        existing.update(updated)
        return existing

    data = await client.query(
        """
        mutation CreateWorkspaceProject(
          $team_id: String!,
          $name: String!,
          $description: String
        ) {
          projectCreate(input: {
            name: $name,
            teamIds: [$team_id],
            description: $description
          }) {
            success
            project { id name url }
          }
        }
        """,
        team_id=team_id,
        name=project_name,
        description=project_description(workspace),
    )
    return payload_entity(data, "projectCreate", "project")


def _duplicate_projects_error(
    projects: list[dict[str, Any]],
    workspace: WorkspaceLike,
) -> LinearBoardError:
    project_ids = ", ".join(sorted(_project_id(project) for project in projects))
    return LinearBoardError(
        f"Duplicate Linear projects for workspace {workspace.folder}: {project_ids}"
    )


def _project_id(project: dict[str, Any]) -> str:
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise LinearBoardError("Linear workspace project did not include an ID")
    return project_id


async def _update_project(
    client: LinearQueryClient,
    project: dict[str, Any],
    workspace: WorkspaceLike,
) -> dict[str, Any]:
    data = await client.query(
        """
        mutation UpdateWorkspaceProject(
          $project_id: String!,
          $name: String!,
          $description: String
        ) {
          projectUpdate(
            id: $project_id,
            input: { name: $name, description: $description }
          ) {
            success
            project { id name url description }
          }
        }
        """,
        project_id=project["id"],
        name=workspace_project_name(workspace),
        description=project_description(workspace),
    )
    return payload_entity(data, "projectUpdate", "project")
