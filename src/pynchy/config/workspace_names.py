"""Naming helpers for configured workspaces and generated dynamic threads."""

from __future__ import annotations

DYNAMIC_THREAD_DELIMITER = "__thread_"


def parent_workspace_name(workspace_name: str) -> str | None:
    """Return the configured parent for a generated dynamic-thread workspace."""
    parent, delimiter, _thread = workspace_name.partition(DYNAMIC_THREAD_DELIMITER)
    return parent if delimiter and parent else None


def static_workspace_name(workspace_name: str) -> str:
    """Return the config identity for a workspace, collapsing dynamic threads."""
    return parent_workspace_name(workspace_name) or workspace_name
