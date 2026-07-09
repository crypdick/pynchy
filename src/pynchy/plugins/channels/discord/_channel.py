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

import asyncio
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves these runtime annotations.
    Callable,
    Iterable,
)
from datetime import UTC, datetime
from typing import Any

import discord

from pynchy.config.discord_refs import DiscordChatTarget, resolve_discord_chat_target
from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter
from pynchy.logger import logger
from pynchy.state import get_chat_jids_by_name
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves these runtime annotations.
    InboundFetchResult,
    NewMessage,
    OutboundEvent,
    WorkspaceProfile,
)
from pynchy.utils import create_background_task

from ._access import DiscordAccess
from ._ask_user import DiscordAskUserView, build_ask_user_text, supports_interactive_ask_user
from ._chunk import DISCORD_LIMIT, chunk_discord_text
from ._events import DiscordEvents, build_message_metadata, normalized_message_content
from ._ids import channel_jid, dm_jid, is_discord_jid, parse_jid
from ._lifecycle import DiscordLifecycle
from ._lookup import discord_user_names, normalize_discord_channel_name, same_name
from ._provisioning import create_discord_group

_MESSAGE_ID_PREFIX = "discord-"
_TYPING_REFRESH_SECONDS = 8.0


def _require_client(client: Any) -> Any:
    if client is None:
        raise RuntimeError("Discord client is not connected")
    return client


class DiscordChannel:
    """Pynchy ``Channel`` protocol implementation backed by discord.py."""

    prefix_assistant_name: bool = False  # Discord shows the bot's own username
    auto_provision_configured_chats: bool = True

    def __init__(  # noqa: PLR0913, RUF100 - channel constructor is a boundary surface for plugin wiring.
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
        self._typing_tasks: dict[str, asyncio.Task[Any]] = {}
        self._dm_channels: dict[str, Any] = {}
        self._ask_user_views: dict[str, discord.ui.View] = {}

        self.access = DiscordAccess(config)
        self.events = DiscordEvents(self)
        self.lifecycle = DiscordLifecycle(self)

    @property
    def config(self) -> Any:
        return self._config

    # ------------------------------------------------------------------
    # Lifecycle — delegated
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        await self.lifecycle.connect()

    def is_connected(self) -> bool:
        return self.lifecycle.is_connected()

    async def disconnect(self) -> None:
        self._cancel_all_typing_tasks()
        self._dm_channels.clear()
        await self.lifecycle.disconnect()

    async def reconnect(self) -> None:
        self._dm_channels.clear()
        await self.lifecycle.reconnect()

    def prepare_shutdown(self) -> None:
        self._cancel_all_typing_tasks()
        self._dm_channels.clear()
        self.lifecycle.prepare_shutdown()

    # ------------------------------------------------------------------
    # Channel protocol — outbound
    # ------------------------------------------------------------------

    def owns_jid(self, jid: str) -> bool:
        # v1 assumes a single Discord connection; every discord: jid is ours.
        return is_discord_jid(jid)

    def allows_registered_workspace_jid(self, jid: str, *, is_dm: bool) -> bool:
        """Return whether a runtime-registered workspace may bypass chat config."""
        if is_dm or self._config.group_policy != "allowlist" or self.workspaces is None:
            return False
        return jid in self.workspaces()

    async def resolve_chat_jid(self, chat_name: str) -> str | None:
        target = resolve_discord_chat_target(self._config, chat_name)
        if target is None:
            return None
        if target.kind == "direct":
            return await self._resolve_direct_chat_jid(target.target_id)
        return await self._resolve_channel_chat_jid(target)

    async def _resolve_direct_chat_jid(self, user_key: str) -> str | None:
        if user_key.isdecimal():
            return dm_jid(user_key)
        user = self._find_configured_user(user_key)
        if user is not None:
            return dm_jid(str(user.id))
        return await self._find_stored_direct_jid(user_key)

    async def _resolve_channel_chat_jid(self, target: DiscordChatTarget) -> str | None:
        if self.client is not None:
            channel = await self.find_configured_channel(target)
            if channel is not None:
                return channel_jid(str(channel.id))
        if not target.target_id.isdecimal():
            return None
        return channel_jid(target.target_id)

    def _known_users(self) -> Iterable[Any]:
        if self.client is None:
            return ()
        cached_users = tuple(getattr(self.client, "users", ()) or ())
        members: list[Any] = []
        get_all_members = getattr(self.client, "get_all_members", None)
        if callable(get_all_members):
            members.extend(get_all_members())
        else:
            for guild in getattr(self.client, "guilds", []) or []:
                members.extend(getattr(guild, "members", []) or [])
        return (*cached_users, *members)

    def _find_configured_user(self, user_key: str) -> Any | None:
        if self.client is None:
            return None
        if user_key.isdecimal():
            return self.client.get_user(int(user_key))
        return next(
            (
                user
                for user in self._known_users()
                if any(same_name(name, user_key) for name in discord_user_names(user))
            ),
            None,
        )

    async def _find_stored_direct_jid(self, user_key: str) -> str | None:
        matches = [
            jid
            for jid in await get_chat_jids_by_name(user_key)
            if jid.startswith("discord:direct:")
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "Multiple Discord DMs match user name; disambiguate",
                user=user_key,
                matches=matches,
            )
        return None

    async def create_group(self, name: str) -> str:
        return await create_discord_group(self, name)

    async def find_configured_channel(self, target: DiscordChatTarget) -> Any | None:
        guild = await self.find_configured_guild(target)
        if guild is None:
            return None
        channel_key = target.target_id
        if channel_key.isdecimal():
            existing = (
                self.client.get_channel(int(channel_key)) if self.client is not None else None
            )
            if existing is not None:
                return existing
        channel_name = self.configured_channel_name(target)
        for channel in getattr(guild, "text_channels", []):
            if channel_key.isdecimal() and str(channel.id) == channel_key:
                return channel
            if same_name(getattr(channel, "name", None), channel_name):
                return channel
        return None

    async def find_configured_guild(self, target: DiscordChatTarget) -> Any | None:
        client = _require_client(self.client)
        guild_key = target.guild_id or ""
        if guild_key.isdecimal():
            guild = client.get_guild(int(guild_key))
            return guild if guild is not None else await client.fetch_guild(int(guild_key))

        guild_name = self._configured_guild_name(target)
        return next(
            (
                guild
                for guild in getattr(client, "guilds", [])
                if same_name(getattr(guild, "name", None), guild_name)
            ),
            None,
        )

    def _configured_guild_name(self, target: DiscordChatTarget) -> str:
        guild_key = target.guild_id or ""
        guild_cfg = self._config.chat.get(guild_key)
        return (guild_cfg.name if guild_cfg and guild_cfg.name else guild_key).strip()

    def configured_channel_name(self, target: DiscordChatTarget) -> str:
        guild_cfg = self._config.chat.get(target.guild_id or "")
        channel_cfg = guild_cfg.channels.get(target.target_id) if guild_cfg else None
        raw = channel_cfg.name if channel_cfg and channel_cfg.name else target.target_id
        return normalize_discord_channel_name(raw)

    async def _resolve_channel(self, jid: str) -> Any:
        """Resolve a jid to a sendable discord.py channel (creating a DM if needed)."""
        client = _require_client(self.client)
        parsed = parse_jid(jid)
        snowflake = int(parsed.snowflake)
        if parsed.kind == "direct":
            cached_dm = self._dm_channels.get(parsed.snowflake)
            if cached_dm is not None:
                return cached_dm
            user = client.get_user(snowflake) or await client.fetch_user(snowflake)
            dm_channel = user.dm_channel or await user.create_dm()
            self._dm_channels[parsed.snowflake] = dm_channel
            return dm_channel
        channel = client.get_channel(snowflake)
        if channel is None:
            channel = await client.fetch_channel(snowflake)
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

    async def send_reaction(self, jid: str, message_id: str, _sender: str, emoji: str) -> None:
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

    def processing_ack_emoji(self) -> str | None:
        """Reaction to use when a message enters processing, or None to disable."""
        emoji = self._config.processing_ack_emoji
        return emoji if isinstance(emoji, str) or emoji is None else str(emoji)

    async def set_typing(self, jid: str, *, is_typing: bool) -> None:
        """Keep Discord's transient typing signal alive while work is active."""
        if self.client is None or not self.owns_jid(jid):
            return

        if not is_typing:
            self._cancel_typing_task(jid)
            return

        task = self._typing_tasks.get(jid)
        if task is not None and not task.done():
            return

        self._typing_tasks[jid] = create_background_task(
            self._typing_loop(jid),
            name=f"discord-typing-{jid[-18:]}",
        )

    async def _typing_loop(self, jid: str) -> None:
        """Refresh typing until cancelled.

        Discord only shows typing for about 10 seconds per signal. This loop
        re-sends the lease while the orchestrator keeps the conversation marked
        active, and it resolves the channel on each pass so reconnects don't
        leave us holding a stale channel object.
        """
        try:
            while True:
                channel = await self._resolve_channel(jid)
                await channel.typing()
                await asyncio.sleep(_TYPING_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise
        except discord.DiscordException as exc:
            logger.debug("Discord typing refresh failed", jid=jid, err=str(exc))
        finally:
            current = self._typing_tasks.get(jid)
            if current is asyncio.current_task():
                self._typing_tasks.pop(jid, None)

    def _cancel_typing_task(self, jid: str) -> None:
        task = self._typing_tasks.pop(jid, None)
        if task is not None:
            task.cancel()

    def _cancel_all_typing_tasks(self) -> None:
        for jid in list(self._typing_tasks):
            self._cancel_typing_task(jid)

    async def send_ask_user(
        self, jid: str, request_id: str, questions: list[dict[str, Any]]
    ) -> str | None:
        """Post an ask_user prompt, using buttons when the prompt shape fits."""
        if self.client is None or not self.owns_jid(jid):
            return None
        text = build_ask_user_text(questions)
        view: DiscordAskUserView | None = None
        if supports_interactive_ask_user(questions):
            view = DiscordAskUserView(
                channel=self,
                jid=jid,
                request_id=request_id,
                questions=questions,
            )
        try:
            channel = await self._resolve_channel(jid)
            message = await channel.send(
                text,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
            )
        except discord.DiscordException as exc:
            logger.warning("Discord ask_user failed", err=str(exc))
            return None
        if view is not None:
            view.bind_message_id(str(message.id))
            self._ask_user_views[str(message.id)] = view
        return f"{_MESSAGE_ID_PREFIX}{message.id}"

    @staticmethod
    def _history_after(since: str) -> discord.Object:
        return discord.Object(id=discord.utils.time_snowflake(datetime.fromisoformat(since)))

    @staticmethod
    def _history_high_water_mark(message: Any, current: str) -> str:
        timestamp = message.created_at.isoformat() if message.created_at else ""
        if timestamp > current:
            return timestamp
        return current

    def _history_message(self, channel_jid: str, message: Any) -> NewMessage | None:
        author = message.author
        if getattr(author, "bot", False) or str(author.id) == self.bot_user_id:
            return None
        timestamp = message.created_at.isoformat() if message.created_at else ""
        return NewMessage(
            id=f"{_MESSAGE_ID_PREFIX}{message.id}",
            chat_jid=channel_jid,
            sender=str(author.id),
            sender_name=getattr(author, "display_name", None) or str(author),
            content=normalized_message_content(message),
            timestamp=timestamp or datetime.now(UTC).isoformat(),
            is_from_me=False,
            metadata=build_message_metadata(message),
        )

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        if self.client is None or not self.owns_jid(channel_jid) or not since:
            return InboundFetchResult(messages=[])
        try:
            channel = await self._resolve_channel(channel_jid)
        except discord.DiscordException:
            return InboundFetchResult(messages=[])

        after = self._history_after(since)
        messages: list[NewMessage] = []
        high_water_mark = ""
        async for message in channel.history(after=after, limit=1000, oldest_first=True):
            high_water_mark = self._history_high_water_mark(message, high_water_mark)
            inbound = self._history_message(channel_jid, message)
            if inbound is not None:
                messages.append(inbound)
        return InboundFetchResult(messages=messages, high_water_mark=high_water_mark)
