"""DiscordChannel — pynchy ``Channel`` protocol backed by discord.py.

Composition root: it owns the shared connection state (the ``discord.Client``,
the bot's user id) and implements the outbound-facing protocol, delegating
inbound handling to :class:`DiscordEvents` and connection management to
:class:`DiscordLifecycle`. Collaborators hold a back-reference to this channel
and read the late-bound ``client`` live, since it is recreated on reconnect.

Outbound text is rendered by the shared ``TextFormatter`` (its markdown renders
natively in Discord) and split by :func:`chunk_discord_text` to respect the
2000-character limit.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import discord

from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter
from pynchy.logger import logger
from pynchy.types import InboundFetchResult, NewMessage, OutboundEvent, WorkspaceProfile

from ._access import DiscordAccess
from ._chunk import chunk_discord_text
from ._events import DiscordEvents
from ._ids import is_discord_jid, parse_jid
from ._lifecycle import DiscordLifecycle

_MESSAGE_ID_PREFIX = "discord-"


class DiscordChannel:
    """Pynchy ``Channel`` protocol implementation backed by discord.py."""

    prefix_assistant_name: bool = False  # Discord shows the bot's own username

    def __init__(
        self,
        connection_name: str,
        config: Any,
        bot_token: str,
        on_message: Callable[[str, NewMessage], None],
        on_chat_metadata: Callable[[str, str, str | None], None],
        on_reaction: Callable[[str, str, str, str], None] | None = None,
        on_ask_user_answer: Callable[[str, dict[str, Any]], None] | None = None,
        workspaces: Callable[[], dict[str, WorkspaceProfile]] | None = None,
    ) -> None:
        self.name = connection_name
        self.formatter = TextFormatter()
        self._config = config
        self._bot_token = bot_token
        # Public callbacks/state so collaborators can reach them via back-ref.
        self.on_message = on_message
        self.on_chat_metadata = on_chat_metadata
        self.on_reaction = on_reaction
        self.on_ask_user_answer = on_ask_user_answer
        self.workspaces = workspaces
        self.bot_user_id: str = ""
        self.client: discord.Client | None = None
        self._connected = False
        self._shutting_down = False

        self.access = DiscordAccess(config)
        self.events = DiscordEvents(self)
        self.lifecycle = DiscordLifecycle(self)

    # ------------------------------------------------------------------
    # Lifecycle — delegated
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        await self.lifecycle.connect()

    def is_connected(self) -> bool:
        return self.lifecycle.is_connected()

    async def disconnect(self) -> None:
        await self.lifecycle.disconnect()

    async def reconnect(self) -> None:
        await self.lifecycle.reconnect()

    def prepare_shutdown(self) -> None:
        self.lifecycle.prepare_shutdown()

    # ------------------------------------------------------------------
    # Channel protocol — outbound
    # ------------------------------------------------------------------

    def owns_jid(self, jid: str) -> bool:
        # v1 assumes a single Discord connection; every discord: jid is ours.
        return is_discord_jid(jid)

    async def _resolve_channel(self, jid: str) -> Any:
        """Resolve a jid to a sendable discord.py channel (creating a DM if needed)."""
        assert self.client is not None
        parsed = parse_jid(jid)
        snowflake = int(parsed.snowflake)
        if parsed.kind == "direct":
            user = self.client.get_user(snowflake) or await self.client.fetch_user(snowflake)
            return user.dm_channel or await user.create_dm()
        channel = self.client.get_channel(snowflake)
        if channel is None:
            channel = await self.client.fetch_channel(snowflake)
        return channel

    async def _send_text(self, channel: Any, text: str) -> None:
        for chunk in chunk_discord_text(text):
            await channel.send(
                chunk,
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
            )

    async def send_event(self, jid: str, event: OutboundEvent) -> None:
        if self.client is None or not self.owns_jid(jid):
            return
        rendered = self.formatter.render(event)
        if not rendered.text.strip():
            return
        try:
            channel = await self._resolve_channel(jid)
        except discord.DiscordException as exc:
            logger.warning("Discord send failed to resolve channel", jid=jid, err=str(exc))
            return
        try:
            await self._send_text(channel, rendered.text)
        except discord.Forbidden as exc:
            logger.warning(
                "Discord send forbidden (missing permission or DM blocked)", err=str(exc)
            )

    async def send_reaction(self, jid: str, message_id: str, sender: str, emoji: str) -> None:
        if self.client is None or not self.owns_jid(jid):
            return
        if not message_id.startswith(_MESSAGE_ID_PREFIX):
            return  # not a Discord-originated message id
        raw_id = message_id.removeprefix(_MESSAGE_ID_PREFIX)
        try:
            channel = await self._resolve_channel(jid)
            message = await channel.fetch_message(int(raw_id))
            await message.add_reaction(emoji)
        except discord.DiscordException as exc:
            logger.debug("Discord reaction failed", err=str(exc))

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        """No-op for v1 (mirrors the Slack channel); Discord typing is transient."""

    async def send_ask_user(
        self, jid: str, request_id: str, questions: list[dict[str, Any]]
    ) -> str | None:
        """Post questions as plain text; the user answers in chat (v1 has no widget)."""
        if self.client is None or not self.owns_jid(jid):
            return None
        lines = ["**Question:**", *(f"- {q.get('question', '')}" for q in questions)]
        try:
            channel = await self._resolve_channel(jid)
            await self._send_text(channel, "\n".join(lines))
        except discord.DiscordException as exc:
            logger.warning("Discord ask_user failed", err=str(exc))
        return None

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        if self.client is None or not self.owns_jid(channel_jid) or not since:
            return InboundFetchResult(messages=[])
        try:
            channel = await self._resolve_channel(channel_jid)
        except discord.DiscordException:
            return InboundFetchResult(messages=[])

        after = discord.Object(id=discord.utils.time_snowflake(datetime.fromisoformat(since)))
        messages: list[NewMessage] = []
        high_water_mark = ""
        async for message in channel.history(after=after, limit=1000, oldest_first=True):
            timestamp = message.created_at.isoformat() if message.created_at else ""
            if timestamp > high_water_mark:
                high_water_mark = timestamp
            author = message.author
            if getattr(author, "bot", False) or str(author.id) == self.bot_user_id:
                continue
            messages.append(
                NewMessage(
                    id=f"{_MESSAGE_ID_PREFIX}{message.id}",
                    chat_jid=channel_jid,
                    sender=str(author.id),
                    sender_name=getattr(author, "display_name", None) or str(author),
                    content=message.content,
                    timestamp=timestamp or datetime.now(UTC).isoformat(),
                    is_from_me=False,
                    metadata={"discord_message_id": str(message.id)},
                )
            )
        return InboundFetchResult(messages=messages, high_water_mark=high_water_mark)
