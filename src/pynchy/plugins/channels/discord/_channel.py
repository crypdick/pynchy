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

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import discord

from pynchy.config.discord_refs import DiscordChatTarget, resolve_discord_chat_target
from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter
from pynchy.logger import logger
from pynchy.types import InboundFetchResult, NewMessage, OutboundEvent, WorkspaceProfile

from ._access import DiscordAccess
from ._chunk import DISCORD_LIMIT, chunk_discord_text
from ._events import DiscordEvents
from ._ids import channel_jid, dm_jid, is_discord_jid, parse_jid
from ._lifecycle import DiscordLifecycle

_MESSAGE_ID_PREFIX = "discord-"


def _same_name(left: str | None, right: str | None) -> bool:
    return bool(left and right and left.casefold() == right.casefold())


def _normalize_discord_channel_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    if not normalized:
        raise ValueError("Discord channel name cannot be empty")
    return normalized[:100]


class DiscordChannel:
    """Pynchy ``Channel`` protocol implementation backed by discord.py."""

    prefix_assistant_name: bool = False  # Discord shows the bot's own username
    auto_provision_configured_chats: bool = True

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

    async def resolve_chat_jid(self, chat_name: str) -> str | None:
        target = resolve_discord_chat_target(self._config, chat_name)
        if target is None:
            return None
        if target.kind == "direct":
            return dm_jid(target.target_id)
        if self.client is not None:
            channel = await self._find_configured_channel(target)
            if channel is not None:
                return channel_jid(str(channel.id))
        if not target.target_id.isdecimal():
            return None
        return channel_jid(target.target_id)

    async def create_group(self, name: str) -> str:
        """Create or reuse a configured Discord text channel and return its JID."""
        if self.client is None:
            raise RuntimeError("Discord client is not connected")
        target = resolve_discord_chat_target(self._config, name)
        if target is None or target.kind != "channel":
            raise ValueError(f"Discord chat ref is not a configured guild channel: {name}")

        existing = await self._find_configured_channel(target)
        if existing is not None:
            return channel_jid(str(existing.id))

        guild = await self._find_configured_guild(target)
        if guild is None:
            raise RuntimeError(f"Discord guild not found for configured chat: {name}")

        channel_name = self._configured_channel_name(target)
        channel = await guild.create_text_channel(
            channel_name,
            reason="Pynchy configured workspace channel",
        )
        logger.info(
            "Created Discord channel",
            connection=self.name,
            guild=getattr(guild, "name", None),
            channel=channel_name,
            channel_id=str(channel.id),
        )
        return channel_jid(str(channel.id))

    async def _find_configured_channel(self, target: DiscordChatTarget) -> Any | None:
        guild = await self._find_configured_guild(target)
        if guild is None:
            return None
        channel_key = target.target_id
        if channel_key.isdecimal():
            existing = (
                self.client.get_channel(int(channel_key)) if self.client is not None else None
            )
            if existing is not None:
                return existing
        channel_name = self._configured_channel_name(target)
        for channel in getattr(guild, "text_channels", []):
            if channel_key.isdecimal() and str(channel.id) == channel_key:
                return channel
            if _same_name(getattr(channel, "name", None), channel_name):
                return channel
        return None

    async def _find_configured_guild(self, target: DiscordChatTarget) -> Any | None:
        assert self.client is not None
        guild_key = target.guild_id or ""
        if guild_key.isdecimal():
            guild = self.client.get_guild(int(guild_key))
            if guild is not None:
                return guild
            return await self.client.fetch_guild(int(guild_key))

        guild_name = self._configured_guild_name(target)
        return next(
            (
                guild
                for guild in getattr(self.client, "guilds", [])
                if _same_name(getattr(guild, "name", None), guild_name)
            ),
            None,
        )

    def _configured_guild_name(self, target: DiscordChatTarget) -> str:
        guild_key = target.guild_id or ""
        guild_cfg = self._config.chat.get(guild_key)
        return (guild_cfg.name if guild_cfg and guild_cfg.name else guild_key).strip()

    def _configured_channel_name(self, target: DiscordChatTarget) -> str:
        guild_cfg = self._config.chat.get(target.guild_id or "")
        channel_cfg = guild_cfg.channels.get(target.target_id) if guild_cfg else None
        raw = channel_cfg.name if channel_cfg and channel_cfg.name else target.target_id
        return _normalize_discord_channel_name(raw)

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

    async def post_event(self, jid: str, event: OutboundEvent) -> str | None:
        """Post a streaming preview message and return its id for in-place updates.

        Returns ``None`` when there is nothing to show or the text is too large
        for a single editable message; core then routes it through the chunked
        :meth:`send_event` path instead.
        """
        if self.client is None or not self.owns_jid(jid):
            return None
        text = self.formatter.render(event).text
        if not text.strip() or len(text) > DISCORD_LIMIT:
            return None
        try:
            channel = await self._resolve_channel(jid)
            message = await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
            )
        except discord.DiscordException as exc:
            logger.warning("Discord post_event failed", jid=jid, err=str(exc))
            return None
        return f"{_MESSAGE_ID_PREFIX}{message.id}"

    async def update_event(self, jid: str, message_id: str, event: OutboundEvent) -> None:
        """Edit a previously posted streaming message in place.

        Raises when the text exceeds the single-message limit so ``sender.py``
        falls back to the chunked :meth:`send_event` path.
        """
        if self.client is None or not self.owns_jid(jid):
            return
        if not message_id.startswith(_MESSAGE_ID_PREFIX):
            return
        text = self.formatter.render(event).text
        if len(text) > DISCORD_LIMIT:
            raise ValueError("Discord message exceeds 2000 chars; falling back to chunked send")
        raw_id = message_id.removeprefix(_MESSAGE_ID_PREFIX)
        channel = await self._resolve_channel(jid)
        message = await channel.fetch_message(int(raw_id))
        await message.edit(content=text, allowed_mentions=discord.AllowedMentions.none())

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
