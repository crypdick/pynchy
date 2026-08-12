"""GraphQL connection helpers for the built-in Linear client."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from pydantic import TypeAdapter, ValidationError

from pynchy.plugins.integrations.linear_errors import LinearError


class LinearIssueState(TypedDict):
    id: str
    name: str
    type: str


class LinearIssueTeam(TypedDict):
    id: str
    key: str
    name: str


class LinearIssueProject(TypedDict):
    id: str
    name: str


class LinearIssueSummary(TypedDict):
    """Parsed issue fields returned by list and title-search operations."""

    id: str
    identifier: NotRequired[str]
    title: NotRequired[str]
    url: NotRequired[str]
    priority: NotRequired[int]
    createdAt: NotRequired[str]
    updatedAt: NotRequired[str]
    state: NotRequired[LinearIssueState | None]
    team: NotRequired[LinearIssueTeam | None]
    project: NotRequired[LinearIssueProject | None]


_ISSUE_LIST_ADAPTER: TypeAdapter[list[LinearIssueSummary]] = TypeAdapter(list[LinearIssueSummary])


def issue_connection_nodes(data: object) -> list[LinearIssueSummary]:
    """Parse one GraphQL issues connection at the provider boundary."""
    if not isinstance(data, dict):
        raise LinearError("Linear response did not include issues")
    connection = data.get("issues")
    if not isinstance(connection, dict):
        raise LinearError("Linear response did not include issues")
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise LinearError("Linear response did not include issues.nodes")
    try:
        return _ISSUE_LIST_ADAPTER.validate_python(nodes)
    except ValidationError as exc:
        raise LinearError("Linear issues response contained an invalid node") from exc


def issue_connection_query(operation: str, variable_definitions: str, issue_filter: str) -> str:
    return f"""
        query {operation}($first: Int!{variable_definitions}) {{
          issues(first: $first{issue_filter}) {{
            nodes {{
              id identifier title url priority createdAt updatedAt
              state {{ id name type }}
              team {{ id key name }}
              project {{ id name }}
            }}
          }}
        }}
        """
