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
    voice_jid,
)
from ._models import (
    DiscordAttachment as DiscordAttachment,
)
from ._models import (
    DiscordAuthor as DiscordAuthor,
)
from ._models import (
    DiscordChannelDetails as DiscordChannelDetails,
)
from ._models import (
    DiscordForwardedMessage as DiscordForwardedMessage,
)
from ._models import (
    DiscordInboundMessage as DiscordInboundMessage,
)
from ._models import (
    DiscordInboundReaction as DiscordInboundReaction,
)
from ._models import (
    DiscordReply as DiscordReply,
)
from ._models import (
    parse_discord_message as parse_discord_message,
)
from ._models import (
    parse_discord_reaction as parse_discord_reaction,
)
from ._plugin import DiscordChannelPlugin as DiscordChannelPlugin
from ._voice_client import PynchyVoiceClient as PynchyVoiceClient

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
    "get_settings",
    "group_jid",
    "is_discord_jid",
    "logger",
    "parse_discord_message",
    "parse_discord_reaction",
    "parse_jid",
    "snowflake_of",
    "voice_jid",
]
