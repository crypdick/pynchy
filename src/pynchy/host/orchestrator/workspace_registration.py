"""Register configured workspaces to concrete channel JIDs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from pynchy.config.merge import ResolvedSandboxConfig
from pynchy.config.models import WorkspaceConfig
from pynchy.config.refs import connection_ref_from_parts, parse_chat_ref
from pynchy.config.settings import Settings
from pynchy.logger import logger
from pynchy.state import set_workspace_profile
from pynchy.types import Channel, ServiceTrustConfig, WorkspaceProfile, WorkspaceSecurity


def resolve_display_name(
    folder: str, config: WorkspaceConfig, resolved_repo_access: str | None
) -> str:
    if config.name:
        return config.name
    if resolved_repo_access:
        # Slack channel names can't contain slashes; use double-dash convention.
        return resolved_repo_access.replace("/", "--")
    return folder.replace("-", " ").title()


def _workspace_security(
    config: WorkspaceConfig, resolved: ResolvedSandboxConfig
) -> WorkspaceSecurity:
    services = {}
    if config.security is not None:
        services = {
            service_name: ServiceTrustConfig(
                public_source=service_config.public_source,
                secret_data=service_config.secret_data,
                public_sink=service_config.public_sink,
                dangerous_writes=service_config.dangerous_writes,
            )
            for service_name, service_config in config.security.services.items()
        }
    return WorkspaceSecurity(services=services, contains_secrets=resolved.contains_secrets)


async def ensure_workspace_registered(
    folder: str,
    config: WorkspaceConfig,
    resolved: ResolvedSandboxConfig,
    display_name: str,
    workspaces: dict[str, WorkspaceProfile],
    folder_to_jid: dict[str, str],
    channels: list[Channel],
    settings: Settings,
    register_fn: Callable[[WorkspaceProfile], Awaitable[None]],
) -> str | None:
    """Ensure folder is registered to its configured chat."""
    jid = folder_to_jid.get(folder)
    chat_ref = parse_chat_ref(config.chat)
    connection_name = (
        connection_ref_from_parts(chat_ref.platform, chat_ref.name) if chat_ref else ""
    )
    allow_create = bool(
        settings.command_center.connection and connection_name == settings.command_center.connection
    )

    expected_jid = await _resolve_configured_jid(
        config=config,
        channels=channels,
        allow_create=allow_create,
    )

    if jid is None:
        if expected_jid is None:
            logger.warning("Workspace chat unavailable, skipping registration", folder=folder)
            return None
        jid = expected_jid
        profile = WorkspaceProfile(
            jid=jid,
            name=display_name,
            folder=folder,
            trigger=f"@{settings.agent.name}",
            added_at=datetime.now(UTC).isoformat(),
            is_admin=resolved.is_admin,
            security=_workspace_security(config, resolved),
        )
        await register_fn(profile)
        folder_to_jid[folder] = jid
        logger.info(
            "Registered workspace for configured chat",
            name=display_name,
            folder=folder,
            is_admin=resolved.is_admin,
        )
    elif expected_jid and jid != expected_jid:
        logger.warning(
            "Workspace JID mismatch with configured chat",
            folder=folder,
            registered_jid=jid,
            expected_jid=expected_jid,
        )

    return jid


async def sync_workspace_profile(
    jid: str | None,
    workspaces: dict[str, WorkspaceProfile],
    folder: str,
    display_name: str,
    config: WorkspaceConfig,
    resolved: ResolvedSandboxConfig,
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


async def _resolve_configured_jid(
    *,
    config: WorkspaceConfig,
    channels: list[Channel],
    allow_create: bool,
) -> str | None:
    chat_ref = parse_chat_ref(config.chat)
    if chat_ref is None:
        logger.warning("Invalid chat ref in workspace config", chat=config.chat)
        return None

    connection_name = connection_ref_from_parts(chat_ref.platform, chat_ref.name)
    channel = next((ch for ch in channels if getattr(ch, "name", None) == connection_name), None)
    if channel is None:
        logger.warning("Configured connection not found for workspace", connection=connection_name)
        return None

    jid = await _resolved_chat_jid(channel, connection_name, chat_ref.chat)
    channel_allows_create = bool(
        allow_create or getattr(channel, "auto_provision_configured_chats", False)
    )
    if jid is None and channel_allows_create:
        jid = await _created_chat_jid(channel, connection_name, chat_ref.chat)

    if jid is None:
        logger.warning(
            "Chat not found for workspace", connection=connection_name, chat=chat_ref.chat
        )
    return jid


def _valid_jid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


async def _resolved_chat_jid(channel: Channel, connection_name: str, chat_name: str) -> str | None:
    if not hasattr(channel, "resolve_chat_jid"):
        return None

    try:
        return _valid_jid(await channel.resolve_chat_jid(chat_name))
    except Exception as exc:
        logger.warning(
            "Failed to resolve chat JID",
            connection=connection_name,
            chat=chat_name,
            err=str(exc),
        )
        return None


async def _created_chat_jid(channel: Channel, connection_name: str, chat_name: str) -> str | None:
    if not hasattr(channel, "create_group"):
        return None

    try:
        jid = _valid_jid(await channel.create_group(chat_name))
    except Exception as exc:
        logger.warning(
            "Failed to create chat group for workspace",
            connection=connection_name,
            chat=chat_name,
            err=str(exc),
        )
        return None

    if jid is not None:
        logger.info(
            "Created chat group for workspace",
            connection=connection_name,
            chat=chat_name,
            jid=jid,
        )
    return jid
