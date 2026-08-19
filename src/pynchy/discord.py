"""Discord conversation contracts shared by configuration and its channel adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

DiscordChatKind = Literal["channel", "direct"]
DiscordChannelKind = Literal["text", "voice", "forum"]
DiscordDmPolicy = Literal["open", "allowlist", "disabled"]
DiscordGroupPolicy = Literal["open", "disabled", "allowlist"]


@dataclass(frozen=True)
class DiscordAccessSettings:
    allowed_users: list[str] | None = None


@dataclass(frozen=True)
class DiscordChannelSettings:
    name: str | None = None
    kind: DiscordChannelKind = "text"
    category: str | None = None
    enabled: bool = True
    require_mention: bool | None = None
    users: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    secret_collections: list[str] = field(default_factory=list)
    security: DiscordAccessSettings | None = None


@dataclass(frozen=True)
class DiscordGuildSettings:
    name: str | None = None
    require_mention: bool = True
    users: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    channels: dict[str, DiscordChannelSettings] = field(default_factory=dict)
    security: DiscordAccessSettings | None = None


@dataclass(frozen=True)
class DiscordConnectionSettings:
    bot_token_env: str
    application_id: str | None = None
    processing_ack_emoji: str | None = "🦞"
    default_thread_participants: list[str] = field(default_factory=list)
    dm_policy: DiscordDmPolicy = "allowlist"
    allow_from: list[str] = field(default_factory=list)
    group_policy: DiscordGroupPolicy = "allowlist"
    security: DiscordAccessSettings | None = None
    chat: dict[str, DiscordGuildSettings] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscordChatTarget:
    kind: DiscordChatKind
    target_id: str
    guild_id: str | None = None


def parse_discord_chat_target(chat: str) -> DiscordChatTarget | None:
    """Parse the suffix after ``connection.discord.<name>.chat.``."""
    parts = chat.split(".")
    if len(parts) == 2 and parts[0] == "direct" and parts[1]:
        return DiscordChatTarget(kind="direct", target_id=parts[1])
    if len(parts) == 3 and parts[0] and parts[1] == "channels" and parts[2]:
        return DiscordChatTarget(kind="channel", guild_id=parts[0], target_id=parts[2])
    return None


def _strip_user_prefix(entry: str) -> str:
    for prefix in ("discord:", "user:"):
        if entry.startswith(prefix):
            return entry[len(prefix) :]
    return entry


def discord_chat_ref_error(  # noqa: PLR0911 - each invalid reference needs its exact diagnostic.
    config: DiscordConnectionSettings, chat: str
) -> str | None:
    """Return an explanatory error when a Discord workspace chat ref is invalid."""
    target = parse_discord_chat_target(chat)
    if target is None:
        return "must target direct.<user-key> or <guild>.channels.<channel> for Discord"
    if target.kind == "direct":
        allowed = config.dm_policy == "open" or any(
            entry == "*" or _strip_user_prefix(entry).casefold() == target.target_id.casefold()
            for entry in config.allow_from
        )
        if config.dm_policy == "disabled" or not allowed:
            return f"Discord direct user is not allowed: {target.target_id}"
        return None
    if config.group_policy == "disabled":
        return "Discord guild messages are disabled"
    guild = config.chat.get(target.guild_id or "")
    if guild is None:
        return f"unknown Discord guild: {target.guild_id}"
    channel = guild.channels.get(target.target_id)
    if channel is None:
        return f"unknown Discord channel: {target.guild_id}.channels.{target.target_id}"
    if not channel.enabled:
        return f"disabled Discord channel: {target.guild_id}.channels.{target.target_id}"
    return None


def resolve_discord_chat_target(
    config: DiscordConnectionSettings, chat: str
) -> DiscordChatTarget | None:
    """Return a concrete target when the chat ref is valid for this connection."""
    target = parse_discord_chat_target(chat)
    if target is None or discord_chat_ref_error(config, chat) is not None:
        return None
    return target
