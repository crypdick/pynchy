"""Register configured workspaces to concrete channel JIDs."""

from __future__ import annotations

from collections.abc import (
    Awaitable,  # noqa: TC003, RUF100 - beartype resolves workspace registration annotations at runtime.
    Callable,  # noqa: TC003, RUF100 - beartype resolves workspace registration annotations at runtime.
)
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

from pynchy.config.merge import (
    ResolvedWorkspaceConfig,  # noqa: TC001, RUF100 - beartype resolves workspace registration annotations at runtime.
)
from pynchy.config.models import (
    WorkspaceConfig,  # noqa: TC001, RUF100 - beartype resolves workspace registration annotations at runtime.
)
from pynchy.config.refs import parse_chat_ref
from pynchy.config.settings import (
    Settings,  # noqa: TC001, RUF100 - beartype resolves workspace registration annotations at runtime.
)
from pynchy.logger import logger
from pynchy.state import rebind_workspace_profile, set_workspace_profile
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves workspace registration annotations at runtime.
    Channel,
    RuntimeId,
    ServiceTrustConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
)


@runtime_checkable
class _WorkspaceCreationChannel(Protocol):
    name: str

    async def create_group(self, name: str) -> str: ...


@runtime_checkable
class _WorkspaceResolutionChannel(Protocol):
    name: str

    async def resolve_chat_jid(self, chat_name: str) -> str | None: ...


class _WorkspaceActivityQueue(Protocol):
    def has_activity(self, runtime_id: RuntimeId) -> bool: ...


async def rebind_workspace_runtime(
    profile: WorkspaceProfile,
    workspaces: dict[str, WorkspaceProfile],
    queue: _WorkspaceActivityQueue,
) -> None:
    """Move one workspace to an explicitly authorized thread JID."""
    old_jid = next(
        (
            jid
            for jid, existing in workspaces.items()
            if existing.folder == profile.folder and jid != profile.jid
        ),
        None,
    )
    if old_jid is not None and queue.has_activity(RuntimeId(profile.folder)):
        raise RuntimeError(f"Cannot rebind active workspace {profile.folder!r} from {old_jid!r}")
    persisted_old_jid = await rebind_workspace_profile(profile)
    prior_jid = old_jid or persisted_old_jid
    if prior_jid is not None and prior_jid != profile.jid:
        workspaces.pop(prior_jid, None)
    workspaces[profile.jid] = profile
    logger.info(
        "Workspace rebound",
        folder=profile.folder,
        old_jid=prior_jid,
        jid=profile.jid,
    )


def available_workspace_groups(
    chats: list[dict[str, Any]],
    workspaces: dict[str, WorkspaceProfile],
    channels: list[Channel],
) -> list[dict[str, Any]]:
    """Project visible channel metadata for the agent workspace snapshot."""

    def is_visible(jid: str) -> bool:
        if jid == "__group_sync__":
            return False
        return not channels or any(channel.owns_jid(jid) for channel in channels)

    registered_jids = set(workspaces)
    return [
        {
            "jid": chat["jid"],
            "name": chat["name"],
            "lastActivity": chat["last_message_time"],
            "isRegistered": chat["jid"] in registered_jids,
        }
        for chat in chats
        if is_visible(chat["jid"])
    ]


def resolve_display_name(folder: str) -> str:
    return folder.replace("-", " ").title()


def workspace_security(
    _config: WorkspaceConfig, resolved: ResolvedWorkspaceConfig
) -> WorkspaceSecurity:
    services: dict[str, ServiceTrustConfig] = {}
    return WorkspaceSecurity(
        services=services,
        contains_secrets=resolved.contains_secrets,
        cop_active=resolved.cop_active,
        capabilities=dict(resolved.capabilities),
    )


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

    if config.chat is not None:
        created_jid = await _resolve_configured_chat_jid(folder, config.chat, channels)
    else:
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
        security=workspace_security(config, resolved),
    )
    await register_fn(profile)
    workspaces[created_jid] = profile
    folder_to_jid[folder] = created_jid
    logger.info("Registered configured workspace", folder=folder, jid=created_jid)
    return created_jid


async def _resolve_configured_chat_jid(
    folder: str,
    chat_ref: str,
    channels: list[Channel],
) -> str | None:
    """Resolve a configured chat reference without provisioning another chat."""
    parsed = parse_chat_ref(chat_ref)
    if parsed is None:
        return None
    expected_name = parsed.name
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.name == expected_name
            and isinstance(candidate, _WorkspaceResolutionChannel)
        ),
        None,
    )
    if channel is None:
        logger.warning(
            "Configured workspace chat has no resolving channel",
            folder=folder,
            chat=chat_ref,
            channel=expected_name,
        )
        return None
    resolved_jid = await channel.resolve_chat_jid(parsed.chat)
    if not resolved_jid:
        logger.warning(
            "Configured workspace chat could not be resolved",
            folder=folder,
            chat=chat_ref,
        )
        return None
    return resolved_jid


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
    security = workspace_security(config, resolved)
    if profile.security != security:
        changed["security"] = security
    if not changed:
        return
    updated = replace(profile, **cast("Any", changed))
    workspaces[jid] = updated
    await set_workspace_profile(updated)
    logger.info("Updated workspace profile", folder=folder, changed=list(changed.keys()))
