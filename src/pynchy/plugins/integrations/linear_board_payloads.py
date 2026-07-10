"""Payload parsing helpers for Linear workspace boards."""

from __future__ import annotations

from typing import Any

from pynchy.plugins.integrations.linear_statuses import LINEAR_TODO_STATUSES
from pynchy.plugins.integrations.linear_workspace_names import (
    WorkspaceIdentity,
    project_matches_workspace,
)

_UNKNOWN_TODO_STATUS = "Unknown todo status '{status}'. Expected one of: {allowed}"
_LINEAR_CONNECTION_MISSING = "Linear response did not include {key}"
_LINEAR_NODES_MISSING = "Linear response did not include {key}.nodes"
_LINEAR_PAYLOAD_INCOMPLETE = "Linear did not complete {payload_key}"
_LINEAR_ENTITY_MISSING = "Linear {payload_key} response did not include {entity_key}"


class LinearBoardPayloadError(RuntimeError):
    """Raised when Linear returns an unexpected board payload."""


def projects_for_workspace(
    projects: list[dict[str, Any]],
    workspace: WorkspaceIdentity,
) -> list[dict[str, Any]]:
    return [
        project
        for project in projects
        if project_matches_workspace(project.get("description"), workspace)
    ]


def normalize_status(status: str) -> str:
    key = status.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in LINEAR_TODO_STATUSES:
        allowed = ", ".join(LINEAR_TODO_STATUSES)
        raise LinearBoardPayloadError(_UNKNOWN_TODO_STATUS.format(status=status, allowed=allowed))
    return key


def norm_name(value: object) -> str:
    return str(value or "").strip().lower()


def nodes(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    connection = data.get(key)
    if not isinstance(connection, dict):
        raise LinearBoardPayloadError(_LINEAR_CONNECTION_MISSING.format(key=key))
    raw_nodes = connection.get("nodes")
    if not isinstance(raw_nodes, list):
        raise LinearBoardPayloadError(_LINEAR_NODES_MISSING.format(key=key))
    return [node for node in raw_nodes if isinstance(node, dict)]


def payload_entity(data: dict[str, Any], payload_key: str, entity_key: str) -> dict[str, Any]:
    payload = data.get(payload_key)
    if not isinstance(payload, dict) or not payload.get("success"):
        raise LinearBoardPayloadError(_LINEAR_PAYLOAD_INCOMPLETE.format(payload_key=payload_key))
    entity = payload.get(entity_key)
    if not isinstance(entity, dict):
        raise LinearBoardPayloadError(
            _LINEAR_ENTITY_MISSING.format(payload_key=payload_key, entity_key=entity_key)
        )
    return entity
