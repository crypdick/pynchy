"""Built-in Discord channel plugin."""

from pynchy.config import get_settings as get_settings
from pynchy.logger import logger as logger

from ._channel import DiscordChannel as DiscordChannel
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
)
from ._plugin import DiscordChannelPlugin as DiscordChannelPlugin

__all__ = [
    "DISCORD_LIMIT",
    "JID_PREFIX",
    "DiscordChannel",
    "DiscordChannelPlugin",
    "DiscordJid",
    "channel_jid",
    "chunk_discord_text",
    "dm_jid",
    "get_settings",
    "group_jid",
    "is_discord_jid",
    "logger",
    "parse_jid",
    "snowflake_of",
]
