"""Configured Discord guild-channel target resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ._ids import channel_jid, voice_jid

if TYPE_CHECKING:
    from pynchy.config.discord_refs import DiscordChatTarget
    from pynchy.config.models import DiscordConnectionConfig

    from ._channel import DiscordChannel
else:
    DiscordChannel = object
    DiscordChatTarget = object
    DiscordConnectionConfig = object


async def resolve_configured_channel_jid(
    channel: DiscordChannel, target: DiscordChatTarget
) -> str | None:
    """Resolve one configured guild text or voice channel to its internal JID."""
    if channel.client is not None:
        resolved = await channel.find_configured_channel(target)
        if resolved is not None:
            factory = (
                voice_jid
                if configured_channel_kind(channel.config, target) == "voice"
                else channel_jid
            )
            return factory(str(cast("Any", resolved).id))
    if not target.target_id.isdecimal():
        return None
    factory = (
        voice_jid if configured_channel_kind(channel.config, target) == "voice" else channel_jid
    )
    return factory(target.target_id)


def configured_channel_kind(config: DiscordConnectionConfig, target: DiscordChatTarget) -> str:
    """Return the configured transport kind for a guild channel target."""
    guild_cfg = config.chat.get(target.guild_id or "")
    channel_cfg = guild_cfg.channels.get(target.target_id) if guild_cfg else None
    return channel_cfg.kind if channel_cfg is not None else "text"
