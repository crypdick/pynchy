"""Discord workspace channel provisioning helpers."""

from __future__ import annotations

from typing import Any

from pynchy.config.discord_refs import (
    DiscordChatTarget,
    parse_discord_chat_target,
    resolve_discord_chat_target,
)
from pynchy.logger import logger

from ._ids import channel_jid
from ._lookup import normalize_discord_channel_name, same_name

DISCORD_CLIENT_NOT_CONNECTED = "Discord client is not connected"
DISCORD_CHAT_REF_NOT_CONFIGURED_GUILD_CHANNEL = (
    "Discord chat ref is not a configured guild channel: {name}"
)
DISCORD_GUILD_NOT_FOUND_FOR_CONFIGURED_CHAT = (
    "Discord guild not found for configured chat: {guild_id}"
)
DISCORD_GUILD_MESSAGES_DISABLED = "Discord guild messages are disabled"
CONFIGURED_DISCORD_GUILD_NOT_FOUND_FOR_WORKSPACE_PROVISIONING = (
    "Configured Discord guild not found for workspace provisioning"
)
MULTIPLE_DISCORD_GUILDS_CONFIGURED = (
    "Multiple Discord guilds are configured; workspace channel provisioning "
    "needs exactly one guild"
)
DISCORD_BOT_NOT_IN_ANY_GUILD = "Discord bot is not in any guild"
MULTIPLE_DISCORD_GUILDS_AVAILABLE = (
    "Multiple Discord guilds are available; configure one Discord guild for "
    "workspace channel provisioning"
)


def _require_client(client: Any) -> Any:
    if client is None:
        raise RuntimeError(DISCORD_CLIENT_NOT_CONNECTED)
    return client


async def create_discord_group(channel: Any, name: str) -> str:
    """Create or reuse a Discord text channel and return its JID."""
    _require_client(channel.client)
    target = parse_discord_chat_target(name)
    if target is None:
        return await _create_workspace_channel(channel, name)

    target = resolve_discord_chat_target(channel.config, name)
    if target is None or target.kind != "channel":
        raise ValueError(
            DISCORD_CHAT_REF_NOT_CONFIGURED_GUILD_CHANNEL.format(name=name)
        )
    return await _create_configured_channel(channel, target)


async def _create_configured_channel(channel: Any, target: DiscordChatTarget) -> str:
    existing = await channel.find_configured_channel(target)
    if existing is not None:
        return channel_jid(str(existing.id))

    guild = await channel.find_configured_guild(target)
    if guild is None:
        raise RuntimeError(
            DISCORD_GUILD_NOT_FOUND_FOR_CONFIGURED_CHAT.format(
                guild_id=target.guild_id
            )
        )

    channel_name = channel.configured_channel_name(target)
    created = await guild.create_text_channel(
        channel_name,
        reason="Pynchy configured workspace channel",
    )
    _log_created_channel(channel, guild, channel_name, created)
    return channel_jid(str(created.id))


async def _create_workspace_channel(channel: Any, name: str) -> str:
    guild = await _find_workspace_provisioning_guild(channel)
    channel_name = normalize_discord_channel_name(name)
    existing = _find_guild_channel_by_name(guild, channel_name)
    if existing is not None:
        return channel_jid(str(existing.id))

    created = await guild.create_text_channel(
        channel_name,
        reason="Pynchy configured workspace channel",
    )
    _log_created_channel(channel, guild, channel_name, created)
    return channel_jid(str(created.id))


async def _find_workspace_provisioning_guild(channel: Any) -> Any:
    if channel.config.group_policy == "disabled":
        raise ValueError(DISCORD_GUILD_MESSAGES_DISABLED)

    configured_guilds = list(channel.config.chat)
    if len(configured_guilds) == 1:
        target = DiscordChatTarget(kind="channel", guild_id=configured_guilds[0], target_id="")
        guild = await channel.find_configured_guild(target)
        if guild is not None:
            return guild
        raise RuntimeError(
            CONFIGURED_DISCORD_GUILD_NOT_FOUND_FOR_WORKSPACE_PROVISIONING
        )
    if len(configured_guilds) > 1:
        raise RuntimeError(MULTIPLE_DISCORD_GUILDS_CONFIGURED)

    guilds = list(getattr(_require_client(channel.client), "guilds", []) or [])
    if len(guilds) == 1:
        return guilds[0]
    if not guilds:
        raise RuntimeError(DISCORD_BOT_NOT_IN_ANY_GUILD)
    raise RuntimeError(MULTIPLE_DISCORD_GUILDS_AVAILABLE)


def _find_guild_channel_by_name(guild: Any, channel_name: str) -> Any | None:
    return next(
        (
            channel
            for channel in getattr(guild, "text_channels", [])
            if same_name(getattr(channel, "name", None), channel_name)
        ),
        None,
    )


def _log_created_channel(channel: Any, guild: Any, channel_name: str, created: Any) -> None:
    logger.info(
        "Created Discord channel",
        connection=channel.name,
        guild=getattr(guild, "name", None),
        channel=channel_name,
        channel_id=str(created.id),
    )
