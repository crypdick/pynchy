"""Linear workspace todo-board helpers.

Pynchy treats each workspace as the stable owner of a todo board.  Linear does
not expose "boards" as a separate API object, so a workspace board is a Linear
Project plus shared team workflow states.
"""

from __future__ import annotations

from collections.abc import (
    Iterable,  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
)
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pynchy.plugins.integrations.linear_statuses import LINEAR_TODO_STATUSES
from pynchy.plugins.integrations.linear_workspace_names import (
    project_description,
    project_matches_workspace,
    todo_description,
    workspace_project_name,
)

_TEAM_PAYLOAD_NOT_OBJECT = "Linear team payload was not an object"
_TEAM_PAYLOAD_MISSING_ID = "Linear team payload missing string id"
_TEAM_KEY_NOT_VISIBLE = "LINEAR_TEAM_KEY did not match a visible Linear team: {team_key}"
_NO_VISIBLE_TEAMS = "Linear API key cannot see any teams"
_LINEAR_PROJECT_MISSING = "Linear response did not include project"
_LINEAR_TEAM_MISSING = "Linear response did not include team"
_UNKNOWN_TODO_STATUS = "Unknown todo status '{status}'. Expected one of: {allowed}"
_LINEAR_CONNECTION_MISSING = "Linear response did not include {key}"
_LINEAR_NODES_MISSING = "Linear response did not include {key}.nodes"
_LINEAR_PAYLOAD_INCOMPLETE = "Linear did not complete {payload_key}"
_LINEAR_ENTITY_MISSING = "Linear {payload_key} response did not include {entity_key}"


class LinearBoardError(RuntimeError):
    """Raised when Linear board reconciliation cannot continue."""


@runtime_checkable
class LinearQueryClient(Protocol):
    async def list_teams(self) -> list[dict[str, Any]]:
        """Return Linear teams visible to the configured credential."""

    async def query(self, query: str, **variables: Any) -> dict[str, Any]:
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


async def ensure_workspace_board(
    client: LinearQueryClient,
    workspace: WorkspaceLike,
    *,
    team_key: str | None,
) -> LinearWorkspaceBoard:
    """Ensure Linear has a project and todo workflow states for a workspace."""
    team = await select_team(client, team_key=team_key)
    resources = await _load_team_resources(client, str(team["id"]))
    states = await _ensure_states(client, str(team["id"]), resources["states"])
    project = await _ensure_project(client, str(team["id"]), workspace, resources["projects"])
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
    resources = await _load_team_resources(client, str(team["id"]))
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
    title: str,
    *,
    team_key: str | None,
    status: str = "backlog",
) -> dict[str, Any]:
    """Create a Linear issue in the workspace's board."""
    board = await ensure_workspace_board(client, workspace, team_key=team_key)
    status_key = _normalize_status(status)
    state = board.states[status_key]
    data = await client.query(
        """
        mutation CreateWorkspaceTodo(
          $team_id: String!,
          $project_id: String!,
          $state_id: String!,
          $title: String!,
          $description: String
        ) {
          issueCreate(input: {
            teamId: $team_id,
            projectId: $project_id,
            stateId: $state_id,
            title: $title,
            description: $description
          }) {
            success
            issue {
              id identifier title url
              state { id name type }
              project { id name }
            }
          }
        }
        """,
        team_id=board.team["id"],
        project_id=board.project["id"],
        state_id=state["id"],
        title=title,
        description=todo_description(workspace),
    )
    return _payload_entity(data, "issueCreate", "issue")


async def move_workspace_todo(
    client: LinearQueryClient,
    workspace: WorkspaceLike,
    *,
    issue_id: str,
    status: str,
    team_key: str | None,
) -> dict[str, Any]:
    """Move a Linear todo issue to one of Pynchy's standard statuses."""
    board = await ensure_workspace_board(client, workspace, team_key=team_key)
    status_key = _normalize_status(status)
    state = board.states[status_key]
    data = await client.query(
        """
        mutation MoveWorkspaceTodo($issue_id: String!, $state_id: String!) {
          issueUpdate(id: $issue_id, input: { stateId: $state_id }) {
            success
            issue { id identifier title url state { id name type } }
          }
        }
        """,
        issue_id=issue_id,
        state_id=state["id"],
    )
    return _payload_entity(data, "issueUpdate", "issue")


async def list_workspace_todos(
    client: LinearQueryClient,
    workspace: WorkspaceLike,
    *,
    team_key: str | None,
    include_done: bool = False,
) -> list[dict[str, Any]]:
    """List issues in the workspace's Linear project."""
    board = await ensure_workspace_board(client, workspace, team_key=team_key)
    data = await client.query(
        """
        query ListWorkspaceTodos($project_id: String!) {
          project(id: $project_id) {
            issues {
              nodes {
                id identifier title url priority createdAt updatedAt
                state { id name type }
                project { id name }
              }
            }
          }
        }
        """,
        project_id=board.project["id"],
    )
    project = data.get("project")
    if not isinstance(project, dict):
        raise LinearBoardError(_LINEAR_PROJECT_MISSING)
    issues = _nodes(project, "issues")
    if include_done:
        return issues
    return [issue for issue in issues if (issue.get("state") or {}).get("type") != "completed"]


async def _load_team_resources(
    client: LinearQueryClient,
    team_id: str,
) -> dict[str, list[dict[str, Any]]]:
    data = await client.query(
        """
        query TeamLinearBoardResources($team_id: String!) {
          team(id: $team_id) {
            projects {
              nodes { id name url description }
            }
            states {
              nodes { id name type position }
            }
          }
        }
        """,
        team_id=team_id,
    )
    team = data.get("team")
    if not isinstance(team, dict):
        raise LinearBoardError(_LINEAR_TEAM_MISSING)
    return {
        "projects": _nodes(team, "projects"),
        "states": _nodes(team, "states"),
    }


async def _ensure_states(
    client: LinearQueryClient,
    team_id: str,
    existing_states: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_name = {_norm_name(state.get("name")): state for state in existing_states}
    states: dict[str, dict[str, Any]] = {}
    for key, spec in LINEAR_TODO_STATUSES.items():
        existing = by_name.get(_norm_name(spec.name))
        if existing is not None:
            states[key] = existing
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
        states[key] = _payload_entity(data, "workflowStateCreate", "workflowState")
    return states


async def _ensure_project(
    client: LinearQueryClient,
    team_id: str,
    workspace: WorkspaceLike,
    existing_projects: list[dict[str, Any]],
) -> dict[str, Any]:
    project_name = workspace_project_name(workspace)
    workspace_projects = await _rename_workspace_projects(
        client,
        existing_projects,
        workspace,
        project_name,
    )
    if workspace_projects:
        return workspace_projects[0]

    by_name = {_norm_name(project.get("name")): project for project in existing_projects}
    existing = by_name.get(_norm_name(project_name))
    if existing is not None:
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
    return _payload_entity(data, "projectCreate", "project")


async def _rename_workspace_projects(
    client: LinearQueryClient,
    projects: list[dict[str, Any]],
    workspace: WorkspaceLike,
    project_name: str,
) -> list[dict[str, Any]]:
    workspace_projects = _projects_for_workspace(projects, workspace)
    renamed: list[dict[str, Any]] = []
    for project in workspace_projects:
        if _norm_name(project.get("name")) == _norm_name(project_name):
            renamed.append(project)
            continue
        renamed_project = await _update_project(client, project, workspace)
        project.update(renamed_project)
        renamed.append(project)
    return renamed


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
    return _payload_entity(data, "projectUpdate", "project")


def _projects_for_workspace(
    projects: list[dict[str, Any]],
    workspace: WorkspaceLike,
) -> list[dict[str, Any]]:
    return [
        project
        for project in projects
        if project_matches_workspace(project.get("description"), workspace)
    ]


def _normalize_status(status: str) -> str:
    key = status.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in LINEAR_TODO_STATUSES:
        allowed = ", ".join(LINEAR_TODO_STATUSES)
        raise LinearBoardError(_UNKNOWN_TODO_STATUS.format(status=status, allowed=allowed))
    return key


def _norm_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _nodes(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    connection = data.get(key)
    if not isinstance(connection, dict):
        raise LinearBoardError(_LINEAR_CONNECTION_MISSING.format(key=key))
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise LinearBoardError(_LINEAR_NODES_MISSING.format(key=key))
    return [node for node in nodes if isinstance(node, dict)]


def _payload_entity(data: dict[str, Any], payload_key: str, entity_key: str) -> dict[str, Any]:
    payload = data.get(payload_key)
    if not isinstance(payload, dict) or not payload.get("success"):
        raise LinearBoardError(_LINEAR_PAYLOAD_INCOMPLETE.format(payload_key=payload_key))
    entity = payload.get(entity_key)
    if not isinstance(entity, dict):
        raise LinearBoardError(
            _LINEAR_ENTITY_MISSING.format(payload_key=payload_key, entity_key=entity_key)
        )
    return entity
