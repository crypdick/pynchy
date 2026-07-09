"""Discord-specific chat reference parsing for workspace config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pynchy.config.models import (
    DiscordConnectionConfig,  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.
)

DiscordChatKind = Literal["channel", "direct"]


@dataclass(frozen=True)
class DiscordChatTarget:
    """A concrete Discord conversation target named by a workspace chat ref."""

    kind: DiscordChatKind
    target_id: str
    guild_id: str | None = None


def parse_discord_chat_target(chat: str) -> DiscordChatTarget | None:
    """Parse the suffix after ``connection.discord.<name>.chat.``.

    Supported concrete targets:
    - ``<guild-key>.channels.<channel-key>`` for guild text channels and threads
    - ``direct.<user-key>`` for DMs keyed by a user snowflake or configured name
    """
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


def _direct_user_allowed(config: DiscordConnectionConfig, user_id: str) -> bool:
    if config.dm_policy == "open":
        return True
    if config.dm_policy == "disabled":
        return False
    return any(
        entry == "*" or _strip_user_prefix(entry).casefold() == user_id.casefold()
        for entry in config.allow_from
    )


def discord_chat_ref_error(config: DiscordConnectionConfig, chat: str) -> str | None:
    """Return an explanatory error when a Discord workspace chat ref is invalid."""
    target = parse_discord_chat_target(chat)
    if target is None:
        return "must target direct.<user-key> or <guild>.channels.<channel> for Discord"

    if target.kind == "direct":
        if not _direct_user_allowed(config, target.target_id):
            return f"Discord direct user is not allowed: {target.target_id}"
        return None

    if config.group_policy == "disabled":
        return "Discord guild messages are disabled"
    return _discord_channel_ref_error(config, target)


def _discord_channel_ref_error(
    config: DiscordConnectionConfig, target: DiscordChatTarget
) -> str | None:
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
    config: DiscordConnectionConfig, chat: str
) -> DiscordChatTarget | None:
    """Return a concrete target when the chat ref is valid for this connection."""
    target = parse_discord_chat_target(chat)
    if target is None or discord_chat_ref_error(config, chat) is not None:
        return None
    return target
