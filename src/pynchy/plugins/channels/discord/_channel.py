"""Discord channel adapter for a shared discord.py connection."""

from __future__ import annotations

# allow: file-length - Protocol methods share this connection's live client state.
# Splitting them would obscure the connection-level lifecycle.
import asyncio
from collections.abc import (  # noqa: TC003 - beartype resolves these runtime annotations.
    Awaitable,
    Callable,
    Iterable,
)
from dataclasses import replace
from pathlib import (
    Path,  # noqa: TC003 - beartype resolves this constructor annotation at runtime.
)
from typing import Any, cast

import discord

from pynchy.async_tasks import create_background_task
from pynchy.discord import DiscordChatTarget, DiscordConnectionSettings, resolve_discord_chat_target
from pynchy.host.orchestrator.api import (  # beartype resolves this runtime annotation.
    RenderedMessage,
    TextFormatter,
)
from pynchy.logger import logger
from pynchy.plugins.api import (  # beartype resolves these runtime annotations.
    AudioTranscriptionResult,
    InboundAudioProcessingRequest,
    InboundAudioProcessingResult,
    InboundFetchResult,
    NewMessage,
    OutboundEvent,
    OutboundEventType,
)
from pynchy.plugins.speech.api import (  # noqa: TC001 - beartype resolves this runtime annotation.
    SpeechSynthesisProvider,
)
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves these runtime annotations.
)

from ._access import DiscordAccess, interaction_context
from ._ask_user import DiscordAskUserView, send_ask_user_prompt
from ._chunk import DISCORD_LIMIT
from ._events import SYNTHETIC_USER_PREFIX, DiscordEvents
from ._history import (
    MESSAGE_ID_PREFIX as _MESSAGE_ID_PREFIX,
)
from ._history import (
    history_after,
    history_high_water_mark,
    history_message,
)
from ._ids import channel_jid, dm_jid, is_discord_jid, parse_jid
from ._lifecycle import DiscordLifecycle
from ._lookup import discord_user_names, normalize_discord_channel_name, same_name
from ._models import parse_discord_message
from ._outbound import send_approval, send_text
from ._provisioning import create_discord_group
from ._targets import configured_channel_kind, resolve_configured_channel_jid
from ._voice import DiscordVoiceManager

_TYPING_REFRESH_SECONDS = 8.0
_DISCORD_CLIENT_NOT_CONNECTED = "Discord client is not connected"
_DISCORD_MESSAGE_TOO_LONG = "Discord message exceeds 2000 chars; falling back to chunked send"
_LINEAR_ISSUE_LINK_PREFIX = "Linear issue: "
_LINEAR_PROJECT_LINK_PREFIX = "Linear project: "


def _require_client(client: object | None) -> object:
    if client is None:
        raise RuntimeError(_DISCORD_CLIENT_NOT_CONNECTED)
    return client


class DiscordChannel:
    """Composition root for one Discord connection.

    It owns the late-bound client and delegates inbound events, outbound text,
    and voice lifecycle to focused collaborators.  Those collaborators retain
    a back-reference because Discord recreates the client on reconnect.
    """

    prefix_assistant_name: bool = False  # Discord shows the bot's own username
    auto_provision_configured_chats: bool = True
    supports_direct_ask_user_callbacks: bool = True

    def __init__(  # noqa: PLR0913 - channel constructor is a boundary surface for plugin wiring.
        self,
        connection_name: str,
        config: DiscordConnectionSettings,
        bot_token: str,
        on_message: Callable[[str, NewMessage], None],
        on_chat_metadata: Callable[[str, str, str | None], None],
        *,
        audio_cache_dir: Path,
        on_reaction: Callable[[str, str, str, str], None] | None = None,
        on_ask_user_answer: Callable[[str, dict[str, object]], None] | None = None,
        on_approval_decision: Callable[[str, str, str, str], None] | None = None,
        workspaces: Callable[[], dict[str, WorkspaceProfile]] | None = None,
        speech_synthesizer: SpeechSynthesisProvider | None = None,
        transcribe_audio: Callable[[Path], Awaitable[AudioTranscriptionResult]] | None = None,
        process_inbound_audio: (
            Callable[[InboundAudioProcessingRequest], Awaitable[InboundAudioProcessingResult]]
            | None
        ) = None,
        find_chat_jids_by_name: Callable[[str], Awaitable[list[str]]] | None = None,
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
        self.on_approval_decision = on_approval_decision
        self.workspaces = workspaces
        self._find_chat_jids_by_name = find_chat_jids_by_name
        self.bot_user_id: str = ""
        self.client: object | None = None
        self._connected = False
        self._shutting_down = False
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}
        self._dm_channels: dict[str, object] = {}
        self._ask_user_views: dict[str, discord.ui.View] = {}

        self.access = DiscordAccess(config)
        self.voice = DiscordVoiceManager(self, speech_synthesizer, transcribe_audio)
        self.events = DiscordEvents(self, audio_cache_dir, process_inbound_audio)
        self.lifecycle = DiscordLifecycle(self)

    @property
    def config(self) -> DiscordConnectionSettings:
        return self._config

    @property
    def bot_token(self) -> str:
        return self._bot_token

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value: bool) -> None:
        self._connected = value

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    @shutting_down.setter
    def shutting_down(self, value: bool) -> None:
        self._shutting_down = value

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
        await self.voice.disconnect()
        await self.lifecycle.disconnect()

    async def reconnect(self) -> None:
        self._dm_channels.clear()
        await self.lifecycle.reconnect()

    def prepare_shutdown(self) -> None:
        self._cancel_all_typing_tasks()
        self._dm_channels.clear()
        self.lifecycle.prepare_shutdown()

    async def handle_voice_state_update(
        self,
        member: object,
        before: object,
        after: object,
    ) -> None:
        """Handle Discord's gateway notification that a voice state changed."""
        await self.voice.on_voice_state_update(member, before, after)

    # ------------------------------------------------------------------
    # Channel protocol — outbound
    # ------------------------------------------------------------------

    def owns_jid(self, jid: str) -> bool:
        # v1 assumes a single Discord connection; every discord: jid is ours.
        return is_discord_jid(jid)

    def is_interaction_allowed(self, interaction: object) -> bool:
        """Apply the ordinary inbound access policy to a component click."""
        ctx = interaction_context(interaction)
        if self.access.decide(ctx) == "allow":
            return True
        jid = dm_jid(ctx.author_id) if ctx.is_dm else channel_jid(ctx.channel_id)
        return (
            self.allows_registered_workspace_jid(jid, is_dm=ctx.is_dm)
            and self.access.decide_registered_workspace(ctx) == "allow"
        )

    def allows_registered_workspace_jid(self, jid: str, *, is_dm: bool) -> bool:
        """Return whether runtime registration supplies this guild destination."""
        if is_dm or self._config.group_policy != "allowlist" or self.workspaces is None:
            return False
        return jid in self.workspaces()

    async def resolve_chat_jid(self, chat_name: str) -> str | None:
        target = resolve_discord_chat_target(self._config, chat_name)
        if target is None:
            return None
        if target.kind == "direct":
            return await self._resolve_direct_chat_jid(target.target_id)
        if configured_channel_kind(self._config, target) == "forum":
            return await self.create_group(chat_name)
        return await resolve_configured_channel_jid(self, target)

    async def _resolve_direct_chat_jid(self, user_key: str) -> str | None:
        if user_key.isdecimal():
            return dm_jid(user_key)
        user = self._find_configured_user(user_key)
        if user is not None:
            return dm_jid(str(user.id))
        return await self._find_stored_direct_jid(user_key)

    def _known_users(self) -> Iterable[object]:
        client = cast("Any", self.client)
        cached_users = tuple(getattr(client, "users", ()) or ())
        members: list[object] = []
        get_all_members = getattr(client, "get_all_members", None)
        if callable(get_all_members):
            members.extend(get_all_members())
        else:
            for guild in getattr(client, "guilds", []) or []:
                members.extend(getattr(guild, "members", []) or [])
        return (*cached_users, *members)

    def _find_configured_user(self, user_key: str) -> object | None:
        if self.client is None:
            return None
        return next(
            (
                user
                for user in self._known_users()
                if any(same_name(name, user_key) for name in discord_user_names(user))
            ),
            None,
        )

    async def _find_stored_direct_jid(self, user_key: str) -> str | None:
        find_chat_jids_by_name = self._find_chat_jids_by_name
        if find_chat_jids_by_name is None:
            return None
        matches = [
            jid
            for jid in await find_chat_jids_by_name(user_key)
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

    async def supports_child_threads(self, parent_jid: str) -> bool:
        """Return whether the resolved Discord target can create child threads."""
        parent = await self.resolve_channel(parent_jid)
        return callable(getattr(parent, "create_thread", None))

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str:
        """Create a public child thread and add its default and requested participants."""
        parent = cast("Any", await self.resolve_channel(parent_jid))
        create_thread = getattr(parent, "create_thread", None)
        if not callable(create_thread):
            raise TypeError("Discord target does not support child threads")
        if getattr(parent, "available_tags", None) is not None:
            created = await create_thread(
                name=name,
                content="Pynchy conversation initialized.",
            )
            thread = getattr(created, "thread", created)
            await self._add_thread_participants(
                thread,
                (*self.config.default_thread_participants, *participant_ids),
            )
            return channel_jid(str(thread.id))
        # discord.py defaults TextChannel.create_thread() to a private thread.
        # Child conversations belong to the configured channel's participants,
        # so opt into the public type explicitly.
        thread = await create_thread(name=name, type=discord.ChannelType.public_thread)
        await self._add_thread_participants(
            thread,
            (*self.config.default_thread_participants, *participant_ids),
        )
        send = getattr(parent, "send", None)
        if callable(send):
            try:
                await send(f"Created thread: <#{thread.id}>")
            except discord.HTTPException as exc:
                # A thread link is discoverability help. Its failure must not make
                # a caller retry an otherwise valid child thread.
                logger.warning(
                    "Could not announce Discord thread",
                    thread_id=thread.id,
                    error=str(exc),
                )
        return channel_jid(str(thread.id))

    async def set_thread_kind(self, child_jid: str, kind: str) -> None:
        """Apply exactly one canonical kind tag when the child belongs to a forum."""
        thread = cast("Any", await self.resolve_channel(child_jid))
        parent = getattr(thread, "parent", None)
        if parent is None and self.client is not None:
            parent_id = getattr(thread, "parent_id", None)
            if parent_id is not None:
                parent = cast("Any", self.client).get_channel(parent_id)
        available_tags = getattr(parent, "available_tags", None)
        if available_tags is None:
            return
        tag = next(
            (
                candidate
                for candidate in available_tags
                if same_name(getattr(candidate, "name", None), kind)
            ),
            None,
        )
        if tag is None:
            raise RuntimeError(f"Discord forum lacks required post tag: {kind}")
        applied_tags = list(getattr(thread, "applied_tags", ()) or ())
        if len(applied_tags) == 1 and getattr(applied_tags[0], "id", None) == getattr(
            tag, "id", None
        ):
            return
        edit = getattr(thread, "edit", None)
        if not callable(edit):
            raise TypeError("Discord target does not support forum post tags")
        await edit(applied_tags=[tag])

    async def ensure_thread_link_pinned(self, child_jid: str, url: str) -> None:
        """Pin one canonical Linear issue link in a managed child conversation."""
        thread = cast("Any", await self.resolve_channel(child_jid))
        content = f"{_LINEAR_ISSUE_LINK_PREFIX}{url}"
        pins = getattr(thread, "pins", None)
        if callable(pins):
            pinned_messages = await pins()
            if any(getattr(message, "content", None) == content for message in pinned_messages):
                return
        send = getattr(thread, "send", None)
        if not callable(send):
            raise TypeError("Discord target does not support sending a pinned link")
        message = await send(content)
        pin = getattr(message, "pin", None)
        if not callable(pin):
            raise TypeError("Discord message does not support pinning")
        await pin()

    async def ensure_forum_guidelines_linked(self, parent_jid: str, url: str) -> None:
        """Preserve forum guidance while reconciling its managed Linear project link."""
        forum = cast("Any", await self.resolve_channel(parent_jid))
        if getattr(forum, "available_tags", None) is None:
            return
        topic = getattr(forum, "topic", None)
        existing_lines = str(topic).splitlines() if isinstance(topic, str) else []
        lines = [
            line for line in existing_lines if not line.startswith(_LINEAR_PROJECT_LINK_PREFIX)
        ]
        lines.append(f"{_LINEAR_PROJECT_LINK_PREFIX}{url}")
        updated_topic = "\n".join(lines)
        if topic == updated_topic:
            return
        edit = getattr(forum, "edit", None)
        if not callable(edit):
            raise TypeError("Discord forum does not support posting guidelines")
        await edit(topic=updated_topic)

    async def set_thread_title(self, child_jid: str, title: str) -> None:
        """Update a child thread's visible title."""
        thread = cast("Any", await self.resolve_channel(child_jid))
        if getattr(thread, "name", None) == title:
            return
        edit = getattr(thread, "edit", None)
        if not callable(edit):
            raise TypeError("Discord target does not support thread titles")
        await edit(name=title)

    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        """Find a child thread with *name* below the given parent.

        Declared workspace threads are durable names, so include archived public
        threads before deciding a name is free. A matching archived thread gets
        reopened instead of letting startup create a duplicate sibling.
        """
        parent = cast("Any", await self.resolve_channel(parent_jid))
        guild = getattr(parent, "guild", None)
        active_threads = getattr(guild, "active_threads", None)
        if not callable(active_threads):
            return None
        parent_id = getattr(parent, "id", None)
        matching_threads = [
            thread
            for thread in await active_threads()
            if getattr(thread, "parent_id", None) == parent_id
            and getattr(thread, "name", None) == name
        ]
        if not matching_threads:
            archived_threads = getattr(parent, "archived_threads", None)
            if callable(archived_threads):
                archived_kwargs: dict[str, object] = {"limit": 100}
                if not hasattr(parent, "available_tags"):
                    archived_kwargs["private"] = False
                matching_threads = [
                    thread
                    async for thread in archived_threads(**archived_kwargs)
                    if getattr(thread, "parent_id", None) == parent_id
                    and getattr(thread, "name", None) == name
                ]
        if not matching_threads:
            return None
        # Discord snowflakes are chronologically ordered. The earliest matching
        # thread is the canonical slot if an earlier Pynchy version created a duplicate.
        canonical_thread = min(matching_threads, key=lambda thread: thread.id)
        if getattr(canonical_thread, "archived", False):
            edit = getattr(canonical_thread, "edit", None)
            if callable(edit):
                try:
                    await edit(archived=False)
                except discord.HTTPException as exc:
                    # Reuse the canonical thread rather than creating a duplicate.
                    # The normal send path reports if Discord keeps it unavailable.
                    logger.warning(
                        "Could not reopen archived Discord thread",
                        thread_id=canonical_thread.id,
                        error=str(exc),
                    )
        return channel_jid(str(canonical_thread.id))

    async def add_thread_participants(
        self,
        child_jid: str,
        participant_ids: tuple[str, ...],
    ) -> None:
        """Add participants to an existing child thread."""
        thread = cast("Any", await self.resolve_channel(child_jid))
        await self._add_thread_participants(thread, participant_ids)

    async def set_thread_closed(self, child_jid: str, *, closed: bool) -> None:
        """Map conversation closed state to Discord's thread archive flag."""
        try:
            thread = cast("Any", await self.resolve_channel(child_jid))
        except discord.NotFound:
            if closed:
                return
            raise
        if bool(getattr(thread, "archived", False)) == closed:
            return
        edit = getattr(thread, "edit", None)
        if not callable(edit):
            raise TypeError("Discord target does not support thread lifecycle")
        await edit(archived=closed)

    async def _add_thread_participants(
        self,
        thread: object,
        participant_ids: tuple[str, ...],
    ) -> None:
        add_user = getattr(thread, "add_user", None)
        for participant_id in dict.fromkeys(participant_ids):
            if (
                not participant_id.isdecimal()
                or participant_id == self.bot_user_id
                or not callable(add_user)
            ):
                continue
            try:
                await add_user(discord.Object(id=int(participant_id)))
            except discord.HTTPException as exc:
                # The public thread still exists; failing its caller here could
                # create a duplicate when that caller retries.
                logger.warning(
                    "Could not add participant to Discord thread",
                    thread_id=thread.id,
                    participant_id=participant_id,
                    error=str(exc),
                )

    async def find_configured_channel(self, target: DiscordChatTarget) -> object | None:
        guild = await self.find_configured_guild(target)
        if guild is None:
            return None
        channel_key = target.target_id
        if channel_key.isdecimal():
            existing = (
                cast("Any", self.client).get_channel(int(channel_key))
                if self.client is not None
                else None
            )
            if existing is not None:
                return cast("object", existing)
        channel_name = self.configured_channel_name(target)
        kind = configured_channel_kind(self.config, target)
        if kind == "voice":
            channel_collection = getattr(guild, "voice_channels", [])
        elif kind == "forum":
            channel_collection = getattr(guild, "forums", [])
        else:
            channel_collection = getattr(guild, "text_channels", [])
        for channel in channel_collection:
            if channel_key.isdecimal() and str(channel.id) == channel_key:
                return cast("object", channel)
            if same_name(getattr(channel, "name", None), channel_name):
                return cast("object", channel)
        return None

    async def find_configured_guild(self, target: DiscordChatTarget) -> object | None:
        client = cast("Any", _require_client(self.client))
        guild_key = target.guild_id or ""
        if guild_key.isdecimal():
            guild = client.get_guild(int(guild_key))
            return cast(
                "object",
                guild if guild is not None else await client.fetch_guild(int(guild_key)),
            )

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

    async def resolve_channel(self, jid: str) -> object:
        """Resolve a jid to a sendable discord.py channel (creating a DM if needed)."""
        client = cast("Any", _require_client(self.client))
        parsed = parse_jid(jid)
        snowflake = int(parsed.snowflake)
        if parsed.kind == "direct":
            cached_dm = self._dm_channels.get(parsed.snowflake)
            if cached_dm is not None:
                return cached_dm
            user = client.get_user(snowflake) or await client.fetch_user(snowflake)
            user_like = cast("Any", user)
            dm_channel = user_like.dm_channel or await user_like.create_dm()
            self._dm_channels[parsed.snowflake] = dm_channel
            return dm_channel
        channel = client.get_channel(snowflake)
        if channel is None:
            channel = await client.fetch_channel(snowflake)
        return channel

    async def conversation_exists(self, jid: str) -> bool:
        """Probe Discord directly so deleted or archived threads cannot appear live."""
        client = cast("Any", _require_client(self.client))
        parsed = parse_jid(jid)
        try:
            if parsed.kind == "direct":
                await client.fetch_user(int(parsed.snowflake))
            else:
                channel = await client.fetch_channel(int(parsed.snowflake))
        except discord.NotFound:
            return False
        return parsed.kind == "direct" or not bool(getattr(channel, "archived", False))

    def forget_ask_user_view(self, message_id: str) -> None:
        self._ask_user_views.pop(message_id, None)

    def bind_ask_user_view(self, message_id: str, view: DiscordAskUserView) -> None:
        self._ask_user_views[message_id] = view

    async def send_event(self, jid: str, event: OutboundEvent) -> None:
        if self.client is None or not self.owns_jid(jid):
            return
        if parse_jid(jid).kind == "voice":
            if event.type is OutboundEventType.RESULT:
                await self.voice.speak(jid, event.content)
            return
        rendered = self._render_event(event)
        if not rendered.text.strip():
            return
        text = rendered.text
        if event.metadata.get("synthetic_user_input") is True:
            text = f"{SYNTHETIC_USER_PREFIX} {text}"
        try:
            channel = await self.resolve_channel(jid)
        except discord.DiscordException as exc:
            raise OSError(f"Discord channel resolution failed: {exc}") from exc
        try:
            short_id = event.metadata.get("short_id")
            if event.type is OutboundEventType.APPROVAL and isinstance(short_id, str) and short_id:
                await send_approval(
                    channel,
                    self,
                    jid,
                    text,
                    short_id,
                    allow_remember=event.metadata.get("allow_remember") is True,
                )
            else:
                await send_text(channel, text)
        except discord.Forbidden as exc:
            raise OSError(f"Discord send forbidden: {exc}") from exc

    async def post_event(self, jid: str, event: OutboundEvent) -> str | None:
        """Post a streaming preview message and return its id for in-place updates.

        Returns ``None`` when there is nothing to show or the text is too large
        for a single editable message; core then routes it through the chunked
        :meth:`send_event` path instead.
        """
        if self.client is None or not self.owns_jid(jid):
            return None
        if parse_jid(jid).kind == "voice":
            return None
        text = self._render_event(event).text
        if not text.strip() or len(text) > DISCORD_LIMIT:
            return None
        try:
            channel = await self.resolve_channel(jid)
            if getattr(channel, "available_tags", None) is not None:
                return None
            message = await cast("Any", channel).send(
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
        if parse_jid(jid).kind == "voice":
            return
        if not message_id.startswith(_MESSAGE_ID_PREFIX):
            return
        text = self._render_event(event).text
        if len(text) > DISCORD_LIMIT:
            raise ValueError(_DISCORD_MESSAGE_TOO_LONG)
        raw_id = message_id.removeprefix(_MESSAGE_ID_PREFIX)
        channel = cast("Any", await self.resolve_channel(jid))
        message = await channel.fetch_message(int(raw_id))
        await cast("Any", message).edit(
            content=text, allowed_mentions=discord.AllowedMentions.none()
        )

    def _render_event(self, event: OutboundEvent) -> RenderedMessage:
        """Render with Discord-specific context without mutating the shared event."""
        channel_event = replace(
            event,
            metadata={
                **event.metadata,
                "prefix_assistant_name": self.prefix_assistant_name,
            },
        )
        return self.formatter.render(channel_event)

    async def send_reaction(self, jid: str, message_id: str, _sender: str, emoji: str) -> None:
        if self.client is None or not self.owns_jid(jid):
            return
        if parse_jid(jid).kind == "voice":
            return
        if not message_id.startswith(_MESSAGE_ID_PREFIX):
            return  # not a Discord-originated message id
        raw_id = message_id.removeprefix(_MESSAGE_ID_PREFIX)
        try:
            channel = cast("Any", await self.resolve_channel(jid))
            message = await channel.fetch_message(int(raw_id))
            await cast("Any", message).add_reaction(emoji)
        except discord.DiscordException as exc:
            logger.debug("Discord reaction failed", err=str(exc))

    def processing_ack_emoji(self) -> str | None:
        """Reaction to use when a message enters processing, or None to disable."""
        return self._config.processing_ack_emoji

    async def set_typing(self, jid: str, *, is_typing: bool) -> None:
        """Keep Discord's transient typing signal alive while work is active."""
        if self.client is None or not self.owns_jid(jid):
            return
        if parse_jid(jid).kind == "voice":
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
                channel = await self.resolve_channel(jid)
                if getattr(channel, "available_tags", None) is not None:
                    return
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
        self, jid: str, request_id: str, questions: list[dict[str, object]]
    ) -> str | None:
        if parse_jid(jid).kind == "voice":
            return None
        return await send_ask_user_prompt(self, jid, request_id, questions)

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        if self.client is None or not self.owns_jid(channel_jid) or not since:
            return InboundFetchResult(messages=[])
        if parse_jid(channel_jid).kind == "voice":
            return InboundFetchResult(messages=[])
        try:
            channel = await self.resolve_channel(channel_jid)
        except discord.DiscordException:
            return InboundFetchResult(messages=[])
        history = getattr(channel, "history", None)
        if not callable(history):
            return InboundFetchResult(messages=[])

        after = history_after(since)
        messages: list[NewMessage] = []
        high_water_mark = ""
        async for message in history(after=after, limit=1000, oldest_first=True):
            high_water_mark = history_high_water_mark(message, high_water_mark)
            inbound = history_message(
                channel_jid=channel_jid,
                message=parse_discord_message(message),
                bot_user_id=self.bot_user_id,
            )
            if inbound is not None:
                messages.append(inbound)
        return InboundFetchResult(messages=messages, high_water_mark=high_water_mark)
