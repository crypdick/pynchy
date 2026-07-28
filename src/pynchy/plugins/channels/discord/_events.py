"""Inbound Discord event handling for :class:`DiscordChannel`.

``parse_discord_message`` and ``parse_discord_reaction`` establish the boundary
between discord.py payloads and the access/routing layers. The rest of this
module operates on Pynchy-owned dataclasses rather than SDK objects.

:class:`DiscordEvents` registers the handlers on the channel's
``discord.Client`` and turns allowed messages into pynchy ``NewMessage``
callbacks. It deliberately does **no** per-conversation serialization: pynchy's
core already serializes inbound messages per chat (as the Slack channel relies
on), so an extra queue here would be redundant.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import (
    Awaitable,  # noqa: TC003, RUF100 - beartype resolves these runtime annotations.
    Callable,  # noqa: TC003, RUF100 - beartype resolves these runtime annotations.
)
from datetime import UTC, datetime
from pathlib import (
    Path,  # noqa: TC003, RUF100 - beartype resolves _discord_audio_cache_dir return annotation at runtime.
)
from typing import TYPE_CHECKING, Any

from pynchy.logger import logger
from pynchy.plugins.api import (  # noqa: TC001, RUF100 - beartype resolves these runtime annotations.
    InboundAudioAttachment,
    InboundAudioProcessingRequest,
    InboundAudioProcessingResult,
    NewMessage,
    is_supported_audio_filename,
)

from ._access import InboundContext
from ._ids import channel_jid, dm_jid
from ._models import (
    DiscordAttachment,
    DiscordForwardedMessage,
    DiscordInboundMessage,
    DiscordInboundReaction,
    parse_discord_message,
    parse_discord_reaction,
)

if TYPE_CHECKING:
    from ._channel import DiscordChannel
else:
    # beartype resolves the ``channel: DiscordChannel`` forward ref at call
    # time from this module's globals. ``_channel`` imports this module, so a
    # real runtime import would be circular — bind a permissive substitute so
    # the forward ref resolves (mypy uses the real type from the branch above).
    DiscordChannel = object


def _author_names(message: DiscordInboundMessage) -> frozenset[str]:
    names = {
        value
        for value in (
            message.author.display_name,
            message.author.global_name,
            message.author.name,
            message.author.rendered_name,
        )
        if isinstance(value, str) and value.strip()
    }
    return frozenset(names)


def build_inbound_context(message: DiscordInboundMessage, bot_user_id: str) -> InboundContext:
    """Extract the access-relevant primitives from a parsed Discord message."""
    is_dm = message.guild_id is None
    return InboundContext(
        is_dm=is_dm,
        author_id=message.author.id,
        author_is_bot=message.author.is_bot,
        guild_id=message.guild_id,
        guild_name=message.guild_name,
        channel_id=message.channel.id,
        channel_name=message.channel.name,
        parent_channel_id=message.channel.parent_id,
        parent_channel_name=message.channel.parent_name,
        author_role_ids=message.author.role_ids,
        mentions_bot=bot_user_id in message.mentioned_user_ids,
        author_names=_author_names(message),
    )


def jid_for(ctx: InboundContext) -> str:
    """Build the pynchy jid a message belongs to.

    DMs key off the user snowflake; guild channels and threads key off the
    (thread's own) channel snowflake.
    """
    if ctx.is_dm:
        return dm_jid(ctx.author_id)
    return channel_jid(ctx.channel_id)


def is_thread_created_system_message(message: DiscordInboundMessage) -> bool:
    """Return True for Discord's parent-channel thread starter notice."""
    return message.system_type == "thread_created"


def _attachment_metadata(attachment: DiscordAttachment) -> dict[str, Any]:
    """Normalize a Discord attachment into plain metadata."""
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "url": attachment.url,
        "proxy_url": attachment.proxy_url,
        "content_type": attachment.content_type,
        "size": attachment.size or 0,
        "description": attachment.description,
        "spoiler": attachment.spoiler,
    }


def _audio_attachment_label(attachment: DiscordAttachment) -> str | None:
    content_type = attachment.content_type
    filename = attachment.filename
    is_audio = (
        isinstance(content_type, str) and content_type.startswith("audio/")
    ) or is_supported_audio_filename(filename)
    if not is_audio:
        return None
    return filename if isinstance(filename, str) and filename else "audio attachment"


def _attachment_fallback_content(message: DiscordInboundMessage) -> str:
    audio_labels = [
        label
        for attachment in message.attachments
        if (label := _audio_attachment_label(attachment)) is not None
    ]
    if not audio_labels:
        return ""
    return (
        "[Audio attachment received; transcription is not available yet: "
        + ", ".join(audio_labels)
        + "]"
    )


def _forwarded_snapshot_metadata(snapshot: DiscordForwardedMessage) -> dict[str, Any]:
    return {
        "content": snapshot.content,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        "type": snapshot.type_name,
        "attachments": [_attachment_metadata(attachment) for attachment in snapshot.attachments],
    }


def normalized_message_content(message: DiscordInboundMessage) -> str:
    """Return the best available human-readable text for an inbound message.

    Forwarded Discord messages can arrive with an empty ``message.content`` and
    only a ``message_snapshots`` payload. Fall back to the forwarded snapshot
    text in that case so Pynchy doesn't ingest a blank message.
    """
    if message.content:
        return message.content

    snapshot_texts = [
        snapshot.content.strip()
        for snapshot in message.forwarded_messages
        if snapshot.content.strip()
    ]
    if snapshot_texts:
        return "\n\n".join(snapshot_texts)
    return _attachment_fallback_content(message)


def build_message_metadata(
    message: DiscordInboundMessage, ctx: InboundContext | None = None
) -> dict[str, Any]:
    """Extract Discord-native structure that would otherwise be lost in text."""
    metadata: dict[str, Any] = {"discord_message_id": message.id}
    if ctx is not None:
        metadata["discord_channel_name"] = ctx.channel_name or ""
        if ctx.parent_channel_id is not None:
            metadata["discord_parent_chat_jid"] = channel_jid(ctx.parent_channel_id)
            metadata["discord_parent_channel_name"] = ctx.parent_channel_name or ""

    if message.attachments:
        metadata["attachments"] = [
            _attachment_metadata(attachment) for attachment in message.attachments
        ]

    if message.reply is not None:
        if message.reply.message_id is not None:
            metadata["reply_to_message_id"] = message.reply.message_id
        if message.reply.sender_name is not None:
            metadata["reply_to_sender"] = message.reply.sender_name
        if message.reply.content:
            metadata["reply_to_text"] = message.reply.content

    forwarded = [_forwarded_snapshot_metadata(snapshot) for snapshot in message.forwarded_messages]
    if forwarded:
        metadata["forwarded_messages"] = forwarded

    return metadata


async def _read_attachment_bytes(attachment: DiscordAttachment) -> bytes | None:
    reader = attachment.read
    if not callable(reader):
        return None
    try:
        value = reader()
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, bytes):
            return value
    except Exception as exc:  # noqa: BLE001, RUF100  # allow: exception-handling - bad Discord attachment reads should not drop the message.
        logger.warning("Discord attachment read failed", err=str(exc))
    return None


async def _transcribe_audio_attachments(
    message: DiscordInboundMessage,
    metadata: dict[str, Any],
    content: str,
    *,
    cache_dir: Path,
    process_inbound_audio: Callable[
        [InboundAudioProcessingRequest], Awaitable[InboundAudioProcessingResult]
    ]
    | None,
) -> str:
    attachments = list(message.attachments)
    metadata_attachments = metadata.get("attachments", [])
    if not isinstance(metadata_attachments, list):
        return content

    inbound_attachments: list[InboundAudioAttachment] = []
    for attachment in attachments:
        if _audio_attachment_label(attachment) is None:
            inbound_attachments.append(
                InboundAudioAttachment(
                    id=attachment.id,
                    filename=attachment.filename,
                    content_type=attachment.content_type,
                    size=attachment.size,
                    data=None,
                )
            )
            continue
        inbound_attachments.append(
            InboundAudioAttachment(
                id=attachment.id,
                filename=attachment.filename,
                content_type=attachment.content_type,
                size=attachment.size,
                data=await _read_attachment_bytes(attachment),
            )
        )

    if process_inbound_audio is None:
        return content or _attachment_fallback_content(message)
    result = await process_inbound_audio(
        InboundAudioProcessingRequest(
            attachments=tuple(inbound_attachments),
            content=content,
            fallback_content=_attachment_fallback_content(message),
            cache_dir=cache_dir,
            message_id=message.id,
        )
    )
    for patch in result.metadata_patches:
        if patch.index >= len(metadata_attachments):
            continue
        attachment_metadata = metadata_attachments[patch.index]
        if not isinstance(attachment_metadata, dict):
            continue
        if patch.cached_path is not None:
            attachment_metadata["cached_path"] = patch.cached_path
        attachment_metadata["transcription"] = patch.transcription
    return result.content


class DiscordEvents:
    """Registers inbound handlers on the channel's client and fires callbacks."""

    def __init__(
        self,
        channel: DiscordChannel,
        audio_cache_dir: Path,
        process_inbound_audio: (
            Callable[[InboundAudioProcessingRequest], Awaitable[InboundAudioProcessingResult]]
            | None
        ),
    ) -> None:
        self._channel = channel
        self._audio_cache_dir = audio_cache_dir
        self._process_inbound_audio = process_inbound_audio
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
        async def on_message(message: object) -> None:
            await self.handle_message(message)

        @client.event  # type: ignore[untyped-decorator]
        async def on_raw_reaction_add(payload: object) -> None:
            await self.handle_reaction(payload)

    async def handle_message(self, message: object) -> None:
        """Parse a discord.py message and dispatch its typed Pynchy representation."""
        await self.handle_inbound_message(parse_discord_message(message))

    async def handle_inbound_message(self, message: DiscordInboundMessage) -> None:
        """Dispatch an already-parsed inbound Discord message."""
        ch = self._channel
        if is_thread_created_system_message(message):
            return
        if message.author.id == ch.bot_user_id:
            return  # our own message
        ctx = build_inbound_context(message, ch.bot_user_id)
        if self._dedup(message.id):
            return
        jid = jid_for(ctx)
        if ch.access.decide(ctx) != "allow":
            registered_destination = ch.allows_registered_workspace_jid(jid, is_dm=ctx.is_dm)
            if not registered_destination or ch.access.decide_registered_workspace(ctx) != "allow":
                return

        sender_name = message.author.display_name or message.author.rendered_name
        created = message.created_at
        timestamp = created.isoformat() if created else datetime.now(UTC).isoformat()
        chat_name = message.channel.name or sender_name

        ch.on_chat_metadata(jid, timestamp, chat_name)
        metadata = build_message_metadata(message, ctx)
        content = await _transcribe_audio_attachments(
            message,
            metadata,
            normalized_message_content(message),
            cache_dir=self._audio_cache_dir,
            process_inbound_audio=self._process_inbound_audio,
        )
        msg = NewMessage(
            id=f"discord-{message.id}",
            chat_jid=jid,
            sender=ctx.author_id,
            sender_name=sender_name,
            content=content,
            timestamp=timestamp,
            is_from_me=False,
            metadata=metadata,
        )
        logger.info("Discord inbound message", jid=jid, sender=ctx.author_id)
        ch.on_message(jid, msg)

    async def handle_reaction(self, payload: object) -> None:
        """Parse a discord.py reaction payload and dispatch it."""
        self.handle_inbound_reaction(parse_discord_reaction(payload))

    def handle_inbound_reaction(self, payload: DiscordInboundReaction) -> None:
        """Dispatch an already-parsed inbound Discord reaction."""
        ch = self._channel
        if ch.on_reaction is None:
            return
        if payload.user_id == ch.bot_user_id:
            return
        if payload.guild_id is None:
            return  # DM reactions unsupported in v1 (raw payload lacks the peer id)
        jid = channel_jid(payload.channel_id)
        ch.on_reaction(jid, payload.message_id, payload.user_id, payload.emoji)
