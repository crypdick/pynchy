"""Resolve policy ownership separately from physical child-thread placement."""

from __future__ import annotations

from collections.abc import (  # beartype resolves placement annotations.
    Callable,
    Iterable,
)
from dataclasses import dataclass

from pynchy.workspace.api import (
    WorkspaceProfile,  # beartype resolves placement annotations.
)

type WorkspaceParentResolver = Callable[[str], str | None]
type MissingWorkspaceProfileResolver = Callable[[str, WorkspaceProfile], WorkspaceProfile | None]

_workspace_parent: WorkspaceParentResolver | None = None
_missing_workspace_profile: MissingWorkspaceProfileResolver | None = None


def configure_workspace_placement(
    *,
    workspace_parent: WorkspaceParentResolver,
    missing_workspace_profile: MissingWorkspaceProfileResolver,
) -> None:
    """Inject configured workspace ownership resolution at composition."""
    global _workspace_parent, _missing_workspace_profile  # noqa: PLW0603 - one host process owns one workspace placement policy.
    _workspace_parent = workspace_parent
    _missing_workspace_profile = missing_workspace_profile


@dataclass(frozen=True, slots=True)
class WorkspacePlacement:
    """Policy owner and Discord root that presents its derived work."""

    owner: WorkspaceProfile
    control_parent: WorkspaceProfile


def resolve_workspace_placement(
    workspaces: Iterable[WorkspaceProfile],
    owner_folder: str,
) -> WorkspacePlacement | None:
    """Resolve one semantic workspace without falling back to another policy.

    A semantic child such as ``fam`` keeps its own profile even though Discord
    requires issue and job controls to be sibling threads below
    ``relationships``.  Missing owners or parents fail closed; a broad default
    workspace would silently grant the wrong tools and execution mode.
    """
    by_folder = {workspace.folder: workspace for workspace in workspaces}
    if _workspace_parent is None or _missing_workspace_profile is None:
        raise RuntimeError("workspace placement has not been configured")
    parent_folder = _workspace_parent(owner_folder)
    if parent_folder is None:
        owner = by_folder.get(owner_folder)
        if owner is None:
            return None
        return WorkspacePlacement(owner=owner, control_parent=owner)
    control_parent = by_folder.get(parent_folder)
    if control_parent is None:
        return None
    owner = by_folder.get(owner_folder)
    if owner is None:
        owner = _missing_workspace_profile(owner_folder, control_parent)
        if owner is None:
            return None
    return WorkspacePlacement(owner=owner, control_parent=control_parent)
