"""Discord workspace channel provisioning helpers."""

from __future__ import annotations

from typing import Any, cast

import discord

from pynchy.discord import (
    DiscordChatTarget,
    parse_discord_chat_target,
    resolve_discord_chat_target,
)
from pynchy.logger import logger

from ._ids import channel_jid
from ._lookup import normalize_discord_channel_name, same_name
from ._targets import configured_channel_kind

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
    "Multiple Discord guilds are configured; workspace channel provisioning needs exactly one guild"
)
DISCORD_BOT_NOT_IN_ANY_GUILD = "Discord bot is not in any guild"
MULTIPLE_DISCORD_GUILDS_AVAILABLE = (
    "Multiple Discord guilds are available; configure one Discord guild for "
    "workspace channel provisioning"
)
# NOTE: Update docs/channels/discord.md § Forum Workspaces if these tags change.
FORUM_POST_KINDS = ("issue", "automation", "planning", "testing", "topic")


def _require_client(client: object) -> object:
    if client is None:
        raise RuntimeError(DISCORD_CLIENT_NOT_CONNECTED)
    return client


async def create_discord_group(channel: object, name: str) -> str:
    """Create or reuse a Discord text channel and return its JID."""
    channel_like = cast("Any", channel)
    _require_client(channel_like.client)
    target = parse_discord_chat_target(name)
    if target is None:
        return await _create_workspace_channel(channel, name)

    target = resolve_discord_chat_target(channel.config, name)
    if target is None or target.kind != "channel":
        raise ValueError(DISCORD_CHAT_REF_NOT_CONFIGURED_GUILD_CHANNEL.format(name=name))
    return await _create_configured_channel(channel, target)


async def _create_configured_channel(channel: object, target: DiscordChatTarget) -> str:
    channel_like = cast("Any", channel)
    existing = await channel_like.find_configured_channel(target)
    if existing is not None:
        if configured_channel_kind(channel_like.config, target) == "forum":
            existing = await ensure_forum_configuration(channel_like, target, existing)
        return channel_jid(str(existing.id))

    guild = await channel_like.find_configured_guild(target)
    if guild is None:
        raise RuntimeError(
            DISCORD_GUILD_NOT_FOUND_FOR_CONFIGURED_CHAT.format(guild_id=target.guild_id)
        )

    channel_name = channel_like.configured_channel_name(target)
    if configured_channel_kind(channel_like.config, target) == "forum":
        category = await _ensure_configured_category(channel_like, target, guild)
        created = await guild.create_forum(
            channel_name,
            category=category,
            available_tags=[discord.ForumTag(name=name) for name in FORUM_POST_KINDS],
            reason="Pynchy configured workspace forum",
        )
        _log_created_channel(channel_like, guild, channel_name, created)
        return channel_jid(str(created.id))

    created = await guild.create_text_channel(
        channel_name,
        reason="Pynchy configured workspace channel",
    )
    _log_created_channel(channel_like, guild, channel_name, created)
    return channel_jid(str(created.id))


async def ensure_forum_configuration(
    channel: object,
    target: DiscordChatTarget,
    forum: object,
) -> object:
    """Ensure one configured forum has its category and canonical kind tags."""
    channel_like = cast("Any", channel)
    forum_like = cast("Any", forum)
    options: dict[str, object] = {}

    existing_tags = list(getattr(forum_like, "available_tags", ()) or ())
    known_names = {
        str(getattr(tag, "name", "")).casefold()
        for tag in existing_tags
        if getattr(tag, "name", None)
    }
    missing = [
        discord.ForumTag(name=name)
        for name in FORUM_POST_KINDS
        if name.casefold() not in known_names
    ]
    if missing:
        options["available_tags"] = [*existing_tags, *missing]

    guild = getattr(forum_like, "guild", None)
    if guild is not None:
        category = await _ensure_configured_category(channel_like, target, guild)
        if category is not None and getattr(forum_like, "category_id", None) != getattr(
            category, "id", None
        ):
            options["category"] = category

    if not options:
        return forum
    edit = getattr(forum_like, "edit", None)
    if not callable(edit):
        raise TypeError("Discord forum does not support configuration reconciliation")
    updated = await edit(
        **options,
        reason="Pynchy configured workspace forum reconciliation",
    )
    return updated or forum


async def _ensure_configured_category(
    channel: object,
    target: DiscordChatTarget,
    guild: object,
) -> object | None:
    channel_like = cast("Any", channel)
    guild_like = cast("Any", guild)
    guild_config = channel_like.config.chat.get(target.guild_id or "")
    channel_config = guild_config.channels.get(target.target_id) if guild_config else None
    category_name = channel_config.category if channel_config is not None else None
    if not category_name:
        return None

    existing = next(
        (
            category
            for category in getattr(guild_like, "categories", ())
            if same_name(getattr(category, "name", None), category_name)
        ),
        None,
    )
    if existing is not None:
        return cast("object", existing)
    return cast(
        "object",
        await guild_like.create_category(
            category_name,
            reason="Pynchy configured workspace category",
        ),
    )


async def _create_workspace_channel(channel: object, name: str) -> str:
    channel_like = cast("Any", channel)
    guild = await _find_workspace_provisioning_guild(channel_like)
    channel_name = normalize_discord_channel_name(name)
    existing = _find_guild_channel_by_name(guild, channel_name)
    if existing is not None:
        return channel_jid(str(existing.id))

    created = await guild.create_text_channel(
        channel_name,
        reason="Pynchy configured workspace channel",
    )
    _log_created_channel(channel_like, guild, channel_name, created)
    return channel_jid(str(created.id))


async def _find_workspace_provisioning_guild(channel: object) -> object:
    channel_like = cast("Any", channel)
    if channel_like.config.group_policy == "disabled":
        raise ValueError(DISCORD_GUILD_MESSAGES_DISABLED)

    configured_guilds = list(channel_like.config.chat)
    if len(configured_guilds) == 1:
        target = DiscordChatTarget(kind="channel", guild_id=configured_guilds[0], target_id="")
        guild = await channel_like.find_configured_guild(target)
        if guild is not None:
            return guild
        raise RuntimeError(CONFIGURED_DISCORD_GUILD_NOT_FOUND_FOR_WORKSPACE_PROVISIONING)
    if len(configured_guilds) > 1:
        raise RuntimeError(MULTIPLE_DISCORD_GUILDS_CONFIGURED)

    guilds = list(getattr(_require_client(channel_like.client), "guilds", []) or [])
    if len(guilds) == 1:
        return guilds[0]
    if not guilds:
        raise RuntimeError(DISCORD_BOT_NOT_IN_ANY_GUILD)
    raise RuntimeError(MULTIPLE_DISCORD_GUILDS_AVAILABLE)


def _find_guild_channel_by_name(guild: object, channel_name: str) -> object | None:
    guild_like = cast("Any", guild)
    return next(
        (
            channel
            for channel in getattr(guild_like, "text_channels", [])
            if same_name(getattr(channel, "name", None), channel_name)
        ),
        None,
    )


def _log_created_channel(
    channel: object, guild: object, channel_name: str, created: object
) -> None:
    channel_like = cast("Any", channel)
    guild_like = cast("Any", guild)
    created_like = cast("Any", created)
    logger.info(
        "Created Discord channel",
        connection=channel_like.name,
        guild=getattr(guild_like, "name", None),
        channel=channel_name,
        channel_id=str(created_like.id),
    )
