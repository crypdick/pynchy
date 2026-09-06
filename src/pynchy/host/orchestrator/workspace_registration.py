"""Register configured workspaces to concrete channel JIDs."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
    Sequence,
)
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, NoReturn, Protocol, cast, runtime_checkable

from pynchy.identifiers import (
    RuntimeId,  # beartype resolves workspace registration annotations at runtime.
)
from pynchy.logger import logger
from pynchy.state.api import rebind_workspace_profile, set_workspace_profile
from pynchy.workspace.api import (
    ResolvedWorkspaceConfig,
    # beartype resolves workspace registration annotations at runtime.
    WorkspaceProfile,
    WorkspaceSecurity,
)

type Settings = Any
type WorkspaceConfig = Any


def _unconfigured_chat_ref(_chat_ref: str) -> NoReturn:
    raise RuntimeError("Workspace chat parsing has not been composed")


parse_chat_ref: Callable[[str], Any] = _unconfigured_chat_ref


def configure_workspace_registration_runtime(*, parse_chat_reference: Callable[[str], Any]) -> None:
    """Bind workspace chat parsing at host composition."""
    global parse_chat_ref  # noqa: PLW0603 - one host process owns the configured chat parser.
    parse_chat_ref = parse_chat_reference


@runtime_checkable
class _WorkspaceCreationChannel(Protocol):
    name: str

    async def create_group(self, name: str) -> str: ...


@runtime_checkable
class _WorkspaceChannel(Protocol):
    name: str


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
    current = workspaces.get(profile.jid)
    old_jid = next(
        (
            jid
            for jid, existing in workspaces.items()
            if existing.folder == profile.folder and jid != profile.jid
        ),
        None,
    )
    active_folder: str | None
    if current is not None and current.folder != profile.folder:
        active_folder = current.folder
    else:
        active_folder = profile.folder if old_jid is not None else None
    if active_folder is not None and queue.has_activity(RuntimeId(active_folder)):
        raise RuntimeError(f"Cannot rebind active workspace {active_folder!r}")
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


@runtime_checkable
class _ChannelOwnership(Protocol):
    def owns_jid(self, jid: str) -> bool: ...


def available_workspace_groups(
    chats: list[dict[str, Any]],
    workspaces: dict[str, WorkspaceProfile],
    channels: Sequence[_ChannelOwnership],
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


def workspace_security(resolved: ResolvedWorkspaceConfig) -> WorkspaceSecurity:
    return WorkspaceSecurity(
        contains_secrets=resolved.contains_secrets,
        cop_active=resolved.cop_active,
        capabilities=dict(resolved.capabilities),
    )


async def ensure_workspace_registered(  # noqa: PLR0911,PLR0913 - registration boundary keeps the full workspace creation contract explicit.
    folder: str,
    config: WorkspaceConfig,
    resolved: ResolvedWorkspaceConfig,
    display_name: str,
    workspaces: dict[str, WorkspaceProfile],
    folder_to_jid: dict[str, str],
    channels: Sequence[_WorkspaceChannel],
    settings: Settings,
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
    rebind_fn: Callable[[WorkspaceProfile], Awaitable[None]] | None = None,
) -> str | None:
    """Return or create the concrete chat JID for a configured workspace."""
    jid = folder_to_jid.get(folder)

    if config.chat is not None:
        created_jid = await _resolve_configured_chat_jid(folder, config.chat, channels)
        if created_jid is None:
            return jid
        if jid == created_jid:
            return jid
        if jid is not None:
            existing_target = workspaces.get(created_jid)
            if existing_target is not None and existing_target.folder != folder:
                logger.warning(
                    "Configured workspace target is registered to another workspace",
                    folder=folder,
                    existing_folder=existing_target.folder,
                    jid=created_jid,
                )
                return jid
            if rebind_fn is None:
                logger.warning(
                    "Configured workspace target changed without rebind support",
                    folder=folder,
                    old_jid=jid,
                    new_jid=created_jid,
                )
                return jid
            current_profile = workspaces[jid]
            profile = WorkspaceProfile(
                jid=created_jid,
                name=display_name,
                folder=folder,
                trigger=f"@{settings.agent.name}",
                added_at=current_profile.added_at,
                is_admin=resolved.is_admin,
                security=workspace_security(resolved),
            )
            await rebind_fn(profile)
            workspaces.pop(jid, None)
            workspaces[created_jid] = profile
            folder_to_jid[folder] = created_jid
            return created_jid
    elif jid is not None:
        return jid
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
        except Exception as exc:  # noqa: BLE001 - allow: exception-handling; one workspace must not block startup.
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
        security=workspace_security(resolved),
    )
    await register_fn(profile)
    workspaces[created_jid] = profile
    folder_to_jid[folder] = created_jid
    logger.info("Registered configured workspace", folder=folder, jid=created_jid)
    return created_jid


async def _resolve_configured_chat_jid(
    folder: str,
    chat_ref: str,
    channels: Sequence[_WorkspaceChannel],
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
    channels: Sequence[_WorkspaceChannel], command_center: str | None
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


async def sync_workspace_profile(
    jid: str | None,
    workspaces: dict[str, WorkspaceProfile],
    folder: str,
    display_name: str,
    resolved: ResolvedWorkspaceConfig,
) -> None:
    """Update the stored workspace profile if resolved config changed."""
    if jid is None or jid not in workspaces:
        return
    profile = workspaces[jid]
    updated = replace(
        profile,
        name=display_name,
        is_admin=resolved.is_admin,
        security=workspace_security(resolved),
    )
    if updated == profile:
        return
    workspaces[jid] = updated
    await set_workspace_profile(updated)
    logger.info(
        "Updated workspace profile",
        folder=folder,
        changed=[
            name
            for name in ("name", "is_admin", "security")
            if getattr(profile, name) != getattr(updated, name)
        ],
    )
