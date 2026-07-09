"""Register configured workspaces to concrete channel JIDs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from pynchy.config.merge import ResolvedWorkspaceConfig
from pynchy.config.models import WorkspaceConfig
from pynchy.config.settings import Settings
from pynchy.logger import logger
from pynchy.state import set_workspace_profile
from pynchy.types import Channel, ServiceTrustConfig, WorkspaceProfile, WorkspaceSecurity


def resolve_display_name(
    folder: str, config: WorkspaceConfig, resolved_repo_access: str | None
) -> str:
    assert config
    if resolved_repo_access:
        # Slack channel names can't contain slashes; use double-dash convention.
        return resolved_repo_access.replace("/", "--")
    return folder.replace("-", " ").title()


def _workspace_security(
    config: WorkspaceConfig, resolved: ResolvedWorkspaceConfig
) -> WorkspaceSecurity:
    assert config
    services: dict[str, ServiceTrustConfig] = {}
    return WorkspaceSecurity(services=services, contains_secrets=resolved.contains_secrets)


async def ensure_workspace_registered(
    folder: str,
    config: WorkspaceConfig,
    resolved: ResolvedWorkspaceConfig,
    display_name: str,
    workspaces: dict[str, WorkspaceProfile],
    folder_to_jid: dict[str, str],
    channels: list[Channel],
    settings: Settings,
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
) -> str | None:
    """Return an existing registration for a configured workspace, if present.

    WorkspaceConfig carries profile selections only. Channel provisioning belongs
    to connection/channel runtime plumbing, so reconciliation can update stored
    rows but cannot infer a JID from WorkspaceConfig alone.
    """
    assert config
    assert resolved
    assert channels is not None
    assert settings is not None
    assert register_fn is not None
    jid = folder_to_jid.get(folder)
    if jid is None:
        logger.debug("Workspace has no registered JID; skipping registration", folder=folder)
    return jid


async def sync_workspace_profile(
    jid: str | None,
    workspaces: dict[str, WorkspaceProfile],
    folder: str,
    display_name: str,
    config: WorkspaceConfig,
    resolved: ResolvedWorkspaceConfig,
) -> None:
    """Update the stored workspace profile if resolved config changed."""
    if jid is None or jid not in workspaces:
        return
    profile = workspaces[jid]
    changed: dict[str, Any] = {}
    if profile.name != display_name:
        changed["name"] = display_name
    if profile.is_admin != resolved.is_admin:
        changed["is_admin"] = resolved.is_admin
    security = _workspace_security(config, resolved)
    if profile.security != security:
        changed["security"] = security
    if not changed:
        return
    updated = replace(profile, **changed)
    workspaces[jid] = updated
    await set_workspace_profile(updated)
    logger.info("Updated workspace profile", folder=folder, changed=list(changed.keys()))
