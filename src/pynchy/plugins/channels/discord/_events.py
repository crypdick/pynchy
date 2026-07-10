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

import inspect
import time
from datetime import UTC, datetime
from pathlib import (
    Path,  # noqa: TC003, RUF100 - beartype resolves _discord_audio_cache_dir return annotation at runtime.
)
from typing import TYPE_CHECKING, Any, cast

from pynchy.config import get_settings
from pynchy.host.audio import is_supported_audio_filename
from pynchy.host.inbound_audio import (
    InboundAudioAttachment,
    InboundAudioProcessingRequest,
    process_inbound_audio_attachments,
)
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


def _author_names(author: object) -> frozenset[str]:
    author_like = cast("Any", author)
    names = {
        value
        for value in (
            getattr(author_like, "display_name", None),
            getattr(author_like, "global_name", None),
            getattr(author_like, "name", None),
            str(author_like),
        )
        if isinstance(value, str) and value.strip()
    }
    return frozenset(names)


def build_inbound_context(message: object, bot_user_id: str) -> InboundContext:
    """Extract the access-relevant primitives from a discord.py message."""
    message_like = cast("Any", message)
    author = message_like.author
    guild = message_like.guild
    channel = message_like.channel
    is_dm = guild is None
    parent_id = getattr(channel, "parent_id", None)
    parent = getattr(channel, "parent", None)
    role_ids = frozenset(str(role.id) for role in getattr(author, "roles", []))
    mentions_bot = any(str(user.id) == bot_user_id for user in message_like.mentions)
    return InboundContext(
        is_dm=is_dm,
        author_id=str(author.id),
        author_is_bot=bool(getattr(author, "bot", False)),
        guild_id=None if is_dm else str(guild.id),
        guild_name=None if is_dm else getattr(guild, "name", None),
        channel_id=str(channel.id),
        channel_name=getattr(channel, "name", None),
        parent_channel_id=str(parent_id) if parent_id else None,
        parent_channel_name=getattr(parent, "name", None) if parent is not None else None,
        author_role_ids=role_ids,
        mentions_bot=mentions_bot,
        author_names=_author_names(author),
    )


def jid_for(ctx: InboundContext) -> str:
    """Build the pynchy jid a message belongs to.

    DMs key off the user snowflake; guild channels and threads key off the
    (thread's own) channel snowflake.
    """
    if ctx.is_dm:
        return dm_jid(ctx.author_id)
    return channel_jid(ctx.channel_id)


def _attachment_metadata(attachment: object) -> dict[str, Any]:
    """Normalize a Discord attachment into plain metadata."""
    attachment_like = cast("Any", attachment)
    return {
        "id": str(getattr(attachment_like, "id", "")),
        "filename": getattr(attachment_like, "filename", ""),
        "url": getattr(attachment_like, "url", ""),
        "proxy_url": getattr(attachment_like, "proxy_url", ""),
        "content_type": getattr(attachment_like, "content_type", None),
        "size": getattr(attachment_like, "size", 0),
        "description": getattr(attachment_like, "description", None),
        "spoiler": bool(getattr(attachment_like, "spoiler", False)),
    }


def _audio_attachment_label(attachment: object) -> str | None:
    attachment_like = cast("Any", attachment)
    content_type = getattr(attachment_like, "content_type", None)
    filename = getattr(attachment_like, "filename", "")
    is_audio = (
        isinstance(content_type, str) and content_type.startswith("audio/")
    ) or is_supported_audio_filename(filename)
    if not is_audio:
        return None
    return filename if isinstance(filename, str) and filename else "audio attachment"


def _attachment_fallback_content(message: object) -> str:
    audio_labels = [
        label
        for attachment in getattr(cast("Any", message), "attachments", [])
        if (label := _audio_attachment_label(attachment)) is not None
    ]
    if not audio_labels:
        return ""
    return (
        "[Audio attachment received; transcription is not available yet: "
        + ", ".join(audio_labels)
        + "]"
    )


def _forwarded_snapshot_metadata(snapshot: object) -> dict[str, Any]:
    snapshot_like = cast("Any", snapshot)
    created = getattr(snapshot_like, "created_at", None)
    return {
        "content": getattr(snapshot_like, "content", ""),
        "created_at": created.isoformat() if created else None,
        "type": str(getattr(snapshot_like, "type", "")),
        "attachments": [
            _attachment_metadata(attachment)
            for attachment in getattr(snapshot_like, "attachments", [])
        ],
    }


def normalized_message_content(message: object) -> str:
    """Return the best available human-readable text for an inbound message.

    Forwarded Discord messages can arrive with an empty ``message.content`` and
    only a ``message_snapshots`` payload. Fall back to the forwarded snapshot
    text in that case so Pynchy doesn't ingest a blank message.
    """
    message_like = cast("Any", message)
    content = getattr(message_like, "content", "")
    if content:
        return content

    snapshot_texts = [
        getattr(snapshot, "content", "").strip()
        for snapshot in getattr(message_like, "message_snapshots", [])
        if getattr(snapshot, "content", "").strip()
    ]
    if snapshot_texts:
        return "\n\n".join(snapshot_texts)
    return _attachment_fallback_content(message_like)


def build_message_metadata(message: object, ctx: InboundContext | None = None) -> dict[str, Any]:
    """Extract Discord-native structure that would otherwise be lost in text."""
    message_like = cast("Any", message)
    metadata: dict[str, Any] = {"discord_message_id": str(message_like.id)}
    if ctx is not None:
        metadata["discord_channel_name"] = ctx.channel_name or ""
        if ctx.parent_channel_id is not None:
            metadata["discord_parent_chat_jid"] = channel_jid(ctx.parent_channel_id)
            metadata["discord_parent_channel_name"] = ctx.parent_channel_name or ""

    attachments = getattr(message_like, "attachments", [])
    if attachments:
        metadata["attachments"] = [_attachment_metadata(attachment) for attachment in attachments]

    reference = getattr(message_like, "reference", None)
    if reference is not None:
        reference_message_id = getattr(reference, "message_id", None)
        if reference_message_id is not None:
            metadata["reply_to_message_id"] = str(reference_message_id)
        resolved = getattr(reference, "resolved", None)
        if resolved is not None:
            resolved_like = cast("Any", resolved)
            author = getattr(resolved_like, "author", None)
            if author is not None:
                sender_name = getattr(author, "display_name", None) or str(author)
                metadata["reply_to_sender"] = sender_name
            resolved_content = getattr(resolved_like, "content", "")
            if resolved_content:
                metadata["reply_to_text"] = resolved_content

    forwarded = [
        _forwarded_snapshot_metadata(snapshot)
        for snapshot in getattr(message_like, "message_snapshots", [])
    ]
    if forwarded:
        metadata["forwarded_messages"] = forwarded

    return metadata


def _discord_audio_cache_dir() -> Path:
    return get_settings().data_dir / "media" / "discord"


async def _read_attachment_bytes(attachment: object) -> bytes | None:
    reader = getattr(cast("Any", attachment), "read", None)
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
    message: object,
    metadata: dict[str, Any],
    content: str,
) -> str:
    attachments = list(getattr(cast("Any", message), "attachments", []))
    metadata_attachments = metadata.get("attachments", [])
    if not isinstance(metadata_attachments, list):
        return content

    inbound_attachments: list[InboundAudioAttachment] = []
    for attachment in attachments:
        if _audio_attachment_label(attachment) is None:
            inbound_attachments.append(
                InboundAudioAttachment(
                    id=str(getattr(cast("Any", attachment), "id", "")),
                    filename=str(getattr(cast("Any", attachment), "filename", "")),
                    content_type=getattr(cast("Any", attachment), "content_type", None),
                    size=getattr(cast("Any", attachment), "size", None),
                    data=None,
                )
            )
            continue
        inbound_attachments.append(
            InboundAudioAttachment(
                id=str(getattr(cast("Any", attachment), "id", "")),
                filename=str(getattr(cast("Any", attachment), "filename", "")),
                content_type=getattr(cast("Any", attachment), "content_type", None),
                size=getattr(cast("Any", attachment), "size", None),
                data=await _read_attachment_bytes(attachment),
            )
        )

    result = await process_inbound_audio_attachments(
        InboundAudioProcessingRequest(
            attachments=tuple(inbound_attachments),
            content=content,
            fallback_content=_attachment_fallback_content(message),
            cache_dir=_discord_audio_cache_dir(),
            message_id=str(getattr(cast("Any", message), "id", "message")),
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
        async def on_message(message: object) -> None:
            await self.handle_message(message)

        @client.event  # type: ignore[untyped-decorator]
        async def on_raw_reaction_add(payload: object) -> None:
            await self.handle_reaction(payload)

    async def handle_message(self, message: object) -> None:
        ch = self._channel
        message_like = cast("Any", message)
        if str(message_like.author.id) == ch.bot_user_id:
            return  # our own message
        ctx = build_inbound_context(message_like, ch.bot_user_id)
        if self._dedup(str(message_like.id)):
            return
        jid = jid_for(ctx)
        if ch.access.decide(ctx) != "allow" and not ch.allows_registered_workspace_jid(
            jid, is_dm=ctx.is_dm
        ):
            return

        sender_name = getattr(message_like.author, "display_name", None) or str(message_like.author)
        created = getattr(message_like, "created_at", None)
        timestamp = created.isoformat() if created else datetime.now(UTC).isoformat()
        chat_name = getattr(message_like.channel, "name", None) or sender_name

        ch.on_chat_metadata(jid, timestamp, chat_name)
        metadata = build_message_metadata(message, ctx)
        content = await _transcribe_audio_attachments(
            message_like,
            metadata,
            normalized_message_content(message_like),
        )
        msg = NewMessage(
            id=f"discord-{message_like.id}",
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
        ch = self._channel
        payload_like = cast("Any", payload)
        if ch.on_reaction is None:
            return
        if str(payload_like.user_id) == ch.bot_user_id:
            return
        if payload_like.guild_id is None:
            return  # DM reactions unsupported in v1 (raw payload lacks the peer id)
        jid = channel_jid(payload_like.channel_id)
        emoji = str(payload_like.emoji)
        ch.on_reaction(jid, str(payload_like.message_id), str(payload_like.user_id), emoji)
