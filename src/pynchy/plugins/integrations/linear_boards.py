"""Linear workspace todo-board helpers.

Pynchy treats each workspace as the stable owner of a todo board.  Linear does
not expose "boards" as a separate API object, so a workspace board is a Linear
Project plus shared team workflow states.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


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
class TodoStatusSpec:
    name: str
    type: str
    position: float
    color: str


@dataclass(frozen=True)
class LinearWorkspaceBoard:
    team: dict[str, Any]
    project: dict[str, Any]
    states: dict[str, dict[str, Any]]


LINEAR_TODO_STATUSES: dict[str, TodoStatusSpec] = {
    "backlog": TodoStatusSpec("Backlog", "backlog", 10.0, "#8A8F98"),
    "planning": TodoStatusSpec("Planning", "unstarted", 20.0, "#F2C94C"),
    "ready": TodoStatusSpec("Ready", "unstarted", 30.0, "#56CCF2"),
    "in_progress": TodoStatusSpec("In Progress", "started", 40.0, "#2F80ED"),
    "done": TodoStatusSpec("Done", "completed", 50.0, "#27AE60"),
}


async def select_team(
    client: LinearQueryClient,
    *,
    team_key: str | None,
) -> dict[str, Any]:
    """Select the Linear team to use, defaulting only when unambiguous."""
    teams = await client.list_teams()
    if team_key:
        normalized = team_key.lower()
        for team in teams:
            if str(team.get("key", "")).lower() == normalized:
                return team
            if str(team.get("id", "")).lower() == normalized:
                return team
            if str(team.get("name", "")).lower() == normalized:
                return team
        raise LinearBoardError(f"LINEAR_TEAM_KEY did not match a visible Linear team: {team_key}")

    if len(teams) == 1:
        return teams[0]
    if not teams:
        raise LinearBoardError("Linear API key cannot see any teams")
    visible = ", ".join(
        str(team.get("key") or team.get("name") or team.get("id")) for team in teams
    )
    raise LinearBoardError(
        "Multiple Linear teams are visible; set LINEAR_TEAM_KEY to one of: " + visible
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
    boards: dict[str, LinearWorkspaceBoard] = {}
    for workspace in workspaces:
        boards[workspace.folder] = await ensure_workspace_board(
            client,
            workspace,
            team_key=team_key,
        )
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
        description=_todo_description(workspace),
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
        raise LinearBoardError("Linear response did not include project")
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
              nodes { id name url }
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
        raise LinearBoardError("Linear response did not include team")
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
        description=_project_description(workspace),
    )
    return _payload_entity(data, "projectCreate", "project")


def workspace_project_name(workspace: WorkspaceLike) -> str:
    display_name = workspace.folder.replace("-", " ").replace("_", " ").title()
    return f"Pynchy: {display_name}"


def _project_description(workspace: WorkspaceLike) -> str:
    return (
        "Managed by Pynchy.\n\n"
        f"pynchy.workspace={workspace.folder}\n"
        f"pynchy.chat_jid={workspace.jid}"
    )


def _todo_description(workspace: WorkspaceLike) -> str:
    return (
        "Captured from a Pynchy workspace todo.\n\n"
        f"pynchy.workspace={workspace.folder}\n"
        f"pynchy.chat_jid={workspace.jid}"
    )


def _normalize_status(status: str) -> str:
    key = status.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in LINEAR_TODO_STATUSES:
        allowed = ", ".join(LINEAR_TODO_STATUSES)
        raise LinearBoardError(f"Unknown todo status '{status}'. Expected one of: {allowed}")
    return key


def _norm_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _nodes(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    connection = data.get(key)
    if not isinstance(connection, dict):
        raise LinearBoardError(f"Linear response did not include {key}")
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise LinearBoardError(f"Linear response did not include {key}.nodes")
    return [node for node in nodes if isinstance(node, dict)]


def _payload_entity(data: dict[str, Any], payload_key: str, entity_key: str) -> dict[str, Any]:
    payload = data.get(payload_key)
    if not isinstance(payload, dict) or not payload.get("success"):
        raise LinearBoardError(f"Linear did not complete {payload_key}")
    entity = payload.get(entity_key)
    if not isinstance(entity, dict):
        raise LinearBoardError(f"Linear {payload_key} response did not include {entity_key}")
    return entity
