"""Inbound Discord event handling for :class:`DiscordChannel`.

The pure functions (:func:`build_inbound_context`, :func:`jid_for`) are the
boundary between discord.py message objects and the access/routing layers, so
they carry no ``discord`` import and are unit-tested with duck-typed fakes.

:class:`DiscordEvents` registers the handlers on the channel's
``discord.Client`` and turns allowed messages into pynchy ``NewMessage``
callbacks. It deliberately does **no** per-conversation serialization: pynchy's
core already serializes inbound messages per chat (as the Slack channel relies
on), so an extra queue here would be redundant.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pynchy.logger import logger
from pynchy.types import NewMessage

from ._access import InboundContext
from ._ids import channel_jid, dm_jid

if TYPE_CHECKING:
    from ._channel import DiscordChannel
else:
    # beartype resolves the ``channel: DiscordChannel`` forward ref at call
    # time from this module's globals. ``_channel`` imports this module, so a
    # real runtime import would be circular — bind a permissive substitute so
    # the forward ref resolves (mypy uses the real type from the branch above).
    DiscordChannel = object


def build_inbound_context(message: Any, bot_user_id: str) -> InboundContext:
    """Extract the access-relevant primitives from a discord.py message."""
    author = message.author
    guild = message.guild
    channel = message.channel
    is_dm = guild is None
    parent_id = getattr(channel, "parent_id", None)
    role_ids = frozenset(str(role.id) for role in getattr(author, "roles", []))
    mentions_bot = any(str(user.id) == bot_user_id for user in message.mentions)
    return InboundContext(
        is_dm=is_dm,
        author_id=str(author.id),
        author_is_bot=bool(getattr(author, "bot", False)),
        guild_id=None if is_dm else str(guild.id),
        channel_id=str(channel.id),
        parent_channel_id=str(parent_id) if parent_id else None,
        author_role_ids=role_ids,
        mentions_bot=mentions_bot,
    )


def jid_for(ctx: InboundContext) -> str:
    """Build the pynchy jid a message belongs to.

    DMs key off the user snowflake; guild channels and threads key off the
    (thread's own) channel snowflake.
    """
    if ctx.is_dm:
        return dm_jid(ctx.author_id)
    return channel_jid(ctx.channel_id)


class DiscordEvents:
    """Registers inbound handlers on the channel's client and fires callbacks."""

    def __init__(self, channel: DiscordChannel) -> None:
        self._channel = channel
        self._seen: dict[str, float] = {}
        self._seen_max = 500

    def _dedup(self, message_id: str) -> bool:
        """Return True if ``message_id`` was already seen (gateway redelivery)."""
        now = time.monotonic()
        if message_id in self._seen:
            return True
        if len(self._seen) >= self._seen_max:
            cutoff = now - 120
            self._seen = {mid: seen for mid, seen in self._seen.items() if seen > cutoff}
        self._seen[message_id] = now
        return False

    def register(self) -> None:
        client = self._channel.client

        # discord.py's event decorator is untyped to mypy (discord is
        # ignore_missing_imports), hence the per-handler untyped-decorator ignores.
        @client.event  # type: ignore[untyped-decorator]
        async def on_message(message: Any) -> None:
            await self.handle_message(message)

        @client.event  # type: ignore[untyped-decorator]
        async def on_raw_reaction_add(payload: Any) -> None:
            await self.handle_reaction(payload)

    async def handle_message(self, message: Any) -> None:
        ch = self._channel
        if str(message.author.id) == ch.bot_user_id:
            return  # our own message
        ctx = build_inbound_context(message, ch.bot_user_id)
        if self._dedup(str(message.id)):
            return
        if ch.access.decide(ctx) != "allow":
            return

        jid = jid_for(ctx)
        sender_name = getattr(message.author, "display_name", None) or str(message.author)
        created = getattr(message, "created_at", None)
        timestamp = created.isoformat() if created else datetime.now(UTC).isoformat()
        chat_name = getattr(message.channel, "name", None) or sender_name

        ch.on_chat_metadata(jid, timestamp, chat_name)
        msg = NewMessage(
            id=f"discord-{message.id}",
            chat_jid=jid,
            sender=ctx.author_id,
            sender_name=sender_name,
            content=message.content,
            timestamp=timestamp,
            is_from_me=False,
            metadata={"discord_message_id": str(message.id)},
        )
        logger.info("Discord inbound message", jid=jid, sender=ctx.author_id)
        ch.on_message(jid, msg)

    async def handle_reaction(self, payload: Any) -> None:
        ch = self._channel
        if ch.on_reaction is None:
            return
        if str(payload.user_id) == ch.bot_user_id:
            return
        if payload.guild_id is None:
            return  # DM reactions unsupported in v1 (raw payload lacks the peer id)
        jid = channel_jid(payload.channel_id)
        emoji = str(payload.emoji)
        ch.on_reaction(jid, str(payload.message_id), str(payload.user_id), emoji)
