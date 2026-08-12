"""GraphQL connection helpers for the built-in Linear client."""

from __future__ import annotations

from typing import Any

from pynchy.plugins.integrations.linear_errors import LinearError


def connection_nodes(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    connection = data.get(key)
    if not isinstance(connection, dict):
        raise LinearError(f"Linear response did not include {key}")
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise LinearError(f"Linear response did not include {key}.nodes")
    return [node for node in nodes if isinstance(node, dict)]


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
