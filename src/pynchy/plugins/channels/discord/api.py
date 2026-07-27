"""Public API for the built-in Discord channel plugin."""

from pynchy.logger import logger

from ._channel import DiscordChannel
from ._chunk import DISCORD_LIMIT, chunk_discord_text
from ._ids import (
    JID_PREFIX,
    DiscordJid,
    channel_jid,
    dm_jid,
    group_jid,
    is_discord_jid,
    parse_jid,
    snowflake_of,
    voice_jid,
)
from ._models import (
    DiscordAttachment,
    DiscordAuthor,
    DiscordChannelDetails,
    DiscordForwardedMessage,
    DiscordInboundMessage,
    DiscordInboundReaction,
    DiscordReply,
    parse_discord_message,
    parse_discord_reaction,
)
from ._plugin import DiscordChannelPlugin
from ._voice_client import PynchyVoiceClient

__all__ = [
    "DISCORD_LIMIT",
    "JID_PREFIX",
    "DiscordAttachment",
    "DiscordAuthor",
    "DiscordChannel",
    "DiscordChannelDetails",
    "DiscordChannelPlugin",
    "DiscordForwardedMessage",
    "DiscordInboundMessage",
    "DiscordInboundReaction",
    "DiscordJid",
    "DiscordReply",
    "PynchyVoiceClient",
    "channel_jid",
    "chunk_discord_text",
    "dm_jid",
    "group_jid",
    "is_discord_jid",
    "logger",
    "parse_discord_message",
    "parse_discord_reaction",
    "parse_jid",
    "snowflake_of",
    "voice_jid",
]
