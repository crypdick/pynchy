"""Naming helpers for configured workspaces and generated dynamic threads."""

from __future__ import annotations

from pynchy.conversation.api import dynamic_thread_folder, parent_workspace_name

__all__ = ("dynamic_thread_folder", "parent_workspace_name", "static_workspace_name")


def static_workspace_name(workspace_name: str) -> str:
    """Return the config identity for a workspace, collapsing dynamic threads."""
    return parent_workspace_name(workspace_name) or workspace_name
