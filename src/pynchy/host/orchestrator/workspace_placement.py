"""Resolve policy ownership separately from physical child-thread placement."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves placement annotations.
    Iterable,
)
from dataclasses import dataclass
from datetime import UTC, datetime

from pynchy.config import get_settings
from pynchy.host.orchestrator.workspace_registration import workspace_security
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves placement annotations.
    WorkspaceProfile,
)


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
    settings = get_settings()
    parent_folder = settings.workspace_parent(owner_folder)
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
        config = settings.workspace_config(owner_folder)
        resolved = settings.resolved_workspace_config(owner_folder)
        if config is None or resolved is None:
            return None
        owner = WorkspaceProfile(
            jid=control_parent.jid,
            name=owner_folder.replace("-", " ").title(),
            folder=owner_folder,
            trigger=control_parent.trigger,
            container_config=control_parent.container_config,
            security=workspace_security(config, resolved),
            is_admin=resolved.is_admin,
            added_at=datetime.now(UTC).isoformat(),
        )
    return WorkspacePlacement(owner=owner, control_parent=control_parent)
