"""Read-only resolution of provisioned Linear workspace resources."""

from __future__ import annotations

from typing import Any

from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_board_payloads import norm_name, projects_for_workspace
from pynchy.plugins.integrations.linear_statuses import LINEAR_TODO_STATUSES
from pynchy.plugins.integrations.linear_workspace_names import (
    WorkspaceIdentity,
)

_WORKSPACE_NOT_PROVISIONED = "Linear workspace board has not been provisioned: {workspace}"


def require_todo_states(
    existing_states: list[dict[str, Any]],
    workspace_folder: str,
) -> dict[str, dict[str, Any]]:
    """Resolve the complete managed workflow without creating missing states."""
    by_name = {norm_name(state.get("name")): state for state in existing_states}
    missing = [
        spec.name for spec in LINEAR_TODO_STATUSES.values() if norm_name(spec.name) not in by_name
    ]
    if missing:
        raise LinearBoardError(
            _WORKSPACE_NOT_PROVISIONED.format(workspace=workspace_folder)
            + "; missing workflow states: "
            + ", ".join(missing)
        )
    return {key: by_name[norm_name(spec.name)] for key, spec in LINEAR_TODO_STATUSES.items()}


def require_workspace_project(
    projects: list[dict[str, Any]],
    workspace: WorkspaceIdentity,
) -> dict[str, Any]:
    """Resolve exactly one managed project without mutating provider state."""
    workspace_projects = projects_for_workspace(projects, workspace)
    if not workspace_projects:
        raise LinearBoardError(_WORKSPACE_NOT_PROVISIONED.format(workspace=workspace.folder))
    if len(workspace_projects) > 1:
        project_ids = ", ".join(sorted(_project_id(project) for project in workspace_projects))
        raise LinearBoardError(
            f"Duplicate Linear projects for workspace {workspace.folder}: {project_ids}"
        )
    return workspace_projects[0]


def _project_id(project: dict[str, Any]) -> str:
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise LinearBoardError("Linear workspace project did not include an ID")
    return project_id
