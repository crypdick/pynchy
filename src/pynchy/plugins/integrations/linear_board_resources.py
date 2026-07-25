"""Paginated Linear team-resource loading for workspace boards."""

from __future__ import annotations

import math
from typing import Any, Protocol, runtime_checkable

from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_board_payloads import nodes, payload_entity

_LINEAR_TEAM_MISSING = "Linear response did not include team"
_LINEAR_PROJECT_PAGE_INFO_MISSING = "Linear project response did not include pageInfo"
_LINEAR_PROJECT_PAGE_HAS_NEXT_MISSING = "Linear project pageInfo missing boolean hasNextPage"
_LINEAR_PROJECT_PAGE_CURSOR_MISSING = "Linear project pageInfo missing endCursor"
_LINEAR_PROJECTS_PAGE_SIZE = 50


@runtime_checkable
class LinearResourceQueryClient(Protocol):
    """Minimal client contract needed to load a team's board resources."""

    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        """Run a Linear GraphQL query."""


async def load_team_resources(
    client: LinearResourceQueryClient,
    team_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Load every team project so board ownership checks remain idempotent."""
    projects: list[dict[str, Any]] = []
    states: list[dict[str, Any]] | None = None
    projects_after: str | None = None
    while True:
        data = await client.query(
            """
            query TeamLinearBoardResources(
              $team_id: String!,
              $projects_first: Int!,
              $projects_after: String
            ) {
              team(id: $team_id) {
                projects(first: $projects_first, after: $projects_after) {
                  nodes { id name url description }
                  pageInfo { hasNextPage endCursor }
                }
                states {
                  nodes { id name type position }
                }
              }
            }
            """,
            team_id=team_id,
            projects_first=_LINEAR_PROJECTS_PAGE_SIZE,
            projects_after=projects_after,
        )
        team = data.get("team")
        if not isinstance(team, dict):
            raise LinearBoardError(_LINEAR_TEAM_MISSING)

        projects.extend(nodes(team, "projects"))
        if states is None:
            states = nodes(team, "states")

        projects_after = _next_projects_page_cursor(team)
        if projects_after is None:
            return {"projects": projects, "states": states}


async def reconcile_workflow_state_position(
    client: LinearResourceQueryClient,
    state: dict[str, Any],
    *,
    position: float,
) -> dict[str, Any]:
    """Keep one managed status at its declared position."""
    current = state.get("position")
    if (
        isinstance(current, int | float)
        and not isinstance(current, bool)
        and math.isclose(float(current), position, rel_tol=0.0, abs_tol=1e-9)
    ):
        return state
    state_id = state.get("id")
    if not isinstance(state_id, str) or not state_id:
        raise LinearBoardError("Linear workflow state did not include an ID")
    data = await client.query(
        """
        mutation UpdateWorkflowStatePosition(
          $state_id: String!,
          $position: Float!
        ) {
          workflowStateUpdate(
            id: $state_id,
            input: { position: $position }
          ) {
            success
            workflowState { id name type position }
          }
        }
        """,
        state_id=state_id,
        position=position,
    )
    return payload_entity(data, "workflowStateUpdate", "workflowState")


def _next_projects_page_cursor(team: dict[str, Any]) -> str | None:
    projects = team.get("projects")
    if not isinstance(projects, dict):
        raise LinearBoardError(_LINEAR_PROJECT_PAGE_INFO_MISSING)
    page_info = projects.get("pageInfo")
    if not isinstance(page_info, dict):
        raise LinearBoardError(_LINEAR_PROJECT_PAGE_INFO_MISSING)

    has_next_page = page_info.get("hasNextPage")
    if not isinstance(has_next_page, bool):
        raise LinearBoardError(_LINEAR_PROJECT_PAGE_HAS_NEXT_MISSING)
    if not has_next_page:
        return None

    end_cursor = page_info.get("endCursor")
    if not isinstance(end_cursor, str) or not end_cursor:
        raise LinearBoardError(_LINEAR_PROJECT_PAGE_CURSOR_MISSING)
    return end_cursor
