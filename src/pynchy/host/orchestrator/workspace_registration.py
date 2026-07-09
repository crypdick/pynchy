"""Register configured workspaces to concrete channel JIDs."""

from __future__ import annotations

from collections.abc import (
    Awaitable,  # noqa: TC003, RUF100 - beartype resolves workspace registration annotations at runtime.
    Callable,  # noqa: TC003, RUF100 - beartype resolves workspace registration annotations at runtime.
)
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, cast

from pynchy.config.merge import (
    ResolvedWorkspaceConfig,  # noqa: TC001, RUF100 - beartype resolves workspace registration annotations at runtime.
)
from pynchy.config.models import (
    WorkspaceConfig,  # noqa: TC001, RUF100 - beartype resolves workspace registration annotations at runtime.
)
from pynchy.config.settings import (
    Settings,  # noqa: TC001, RUF100 - beartype resolves workspace registration annotations at runtime.
)
from pynchy.logger import logger
from pynchy.state import set_workspace_profile
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves workspace registration annotations at runtime.
    Channel,
    ServiceTrustConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
)


class _WorkspaceCreationChannel(Protocol):
    name: str

    async def create_group(self, name: str) -> str: ...


def resolve_display_name(folder: str) -> str:
    return folder.replace("-", " ").title()


def _workspace_security(
    _config: WorkspaceConfig, resolved: ResolvedWorkspaceConfig
) -> WorkspaceSecurity:
    services: dict[str, ServiceTrustConfig] = {}
    return WorkspaceSecurity(services=services, contains_secrets=resolved.contains_secrets)


async def ensure_workspace_registered(  # noqa: PLR0913, RUF100 - registration boundary keeps the full workspace creation contract explicit.
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
    """Return or create the concrete chat JID for a configured workspace."""
    jid = folder_to_jid.get(folder)
    if jid is not None:
        return jid

    channel = _workspace_creation_channel(channels, settings.command_center.connection)
    if channel is None:
        logger.warning(
            "Workspace has no registered JID and no creation-capable command center",
            folder=folder,
            command_center=settings.command_center.connection,
        )
        return None

    try:
        created_jid = await channel.create_group(display_name)
    except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; one workspace must not block startup.
        logger.warning(
            "Workspace chat creation failed; skipping registration",
            folder=folder,
            display_name=display_name,
            exc_type=type(exc).__name__,
            err=str(exc),
        )
        return None
    if not isinstance(created_jid, str) or not created_jid:
        logger.warning("Workspace channel creation returned no JID", folder=folder)
        return None

    existing_profile = workspaces.get(created_jid)
    if existing_profile is not None and existing_profile.folder != folder:
        logger.warning(
            "Workspace channel creation returned JID already registered to another workspace",
            folder=folder,
            existing_folder=existing_profile.folder,
            jid=created_jid,
        )
        return None

    profile = WorkspaceProfile(
        jid=created_jid,
        name=display_name,
        folder=folder,
        trigger=f"@{settings.agent.name}",
        added_at=datetime.now(UTC).isoformat(),
        is_admin=resolved.is_admin,
        security=_workspace_security(config, resolved),
    )
    workspaces[created_jid] = profile
    folder_to_jid[folder] = created_jid
    await register_fn(profile)
    logger.info("Registered configured workspace", folder=folder, jid=created_jid)
    return created_jid


def _workspace_creation_channel(
    channels: list[Channel], command_center: str | None
) -> _WorkspaceCreationChannel | None:
    if not command_center:
        return None
    return next(
        (
            cast("_WorkspaceCreationChannel", channel)
            for channel in channels
            if channel.name == command_center and hasattr(channel, "create_group")
        ),
        None,
    )


async def sync_workspace_profile(  # noqa: PLR0913, RUF100 - sync boundary mirrors the stored workspace profile update contract.
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
    changed: dict[str, object] = {}
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
