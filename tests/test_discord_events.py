"""Discord inbound messages as emitted by the public channel adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import discord

from pynchy.config.api import DiscordConnectionConfig
from pynchy.host.audio import AudioTranscriptionResult, process_inbound_audio_attachments
from pynchy.plugins.channels.discord import (
    DiscordAttachment,
    DiscordAuthor,
    DiscordChannel,
    DiscordChannelDetails,
    DiscordForwardedMessage,
    DiscordInboundMessage,
    DiscordReply,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pynchy.plugins.api import (
        InboundAudioProcessingRequest,
        InboundAudioProcessingResult,
        NewMessage,
    )


BOT_ID = "999"
DISCORD_BOT_ENV = "X"


@dataclass
class _ApplicationRole:
    id: str


@dataclass
class _ApplicationUser:
    id: str
    bot: bool
    display_name: str
    global_name: str
    name: str
    roles: list[_ApplicationRole]


@dataclass
class _ApplicationGuild:
    id: str
    name: str


@dataclass
class _ApplicationChannel:
    id: str
    name: str
    parent: object | None
    parent_id: str | None


@dataclass
class _ApplicationResponse:
    send_message: AsyncMock


@dataclass
class _ApplicationInteraction:
    id: int
    user: _ApplicationUser
    guild: _ApplicationGuild
    channel: _ApplicationChannel
    created_at: datetime | None
    response: _ApplicationResponse


def _user(
    uid: str,
    *,
    bot: bool = False,
    roles: tuple[str, ...] = (),
    display_name: str | None = None,
    global_name: str | None = None,
    name: str | None = None,
) -> DiscordAuthor:
    return DiscordAuthor(
        id=uid,
        is_bot=bot,
        role_ids=frozenset(roles),
        display_name=display_name,
        global_name=global_name,
        name=name,
        rendered_name=name or display_name or uid,
    )


def _message(
    *,
    author: DiscordAuthor,
    guild_id: str | None,
    channel_id: str,
    guild_name: str | None = None,
    channel_name: str | None = None,
    parent_id: str | None = None,
    parent_name: str | None = None,
    mentions: tuple[str, ...] = (),
    content: str = "",
    attachments: tuple[DiscordAttachment, ...] = (),
    reference: DiscordReply | None = None,
    message_snapshots: tuple[DiscordForwardedMessage, ...] = (),
    message_type: str | None = None,
) -> DiscordInboundMessage:
    return DiscordInboundMessage(
        id="m1",
        author=author,
        guild_id=guild_id,
        guild_name=guild_name,
        channel=DiscordChannelDetails(
            id=channel_id,
            name=channel_name,
            parent_id=parent_id,
            parent_name=parent_name,
        ),
        content=content,
        attachments=attachments,
        reply=reference,
        forwarded_messages=message_snapshots,
        mentioned_user_ids=frozenset(mentions),
        system_type=message_type,
        created_at=None,
    )


def _attachment(
    *,
    attachment_id: str,
    filename: str,
    url: str = "https://example.invalid/file.txt",
    proxy_url: str = "https://cdn.example.invalid/file.txt",
    content_type: str | None = "text/plain",
    size: int = 12,
    description: str | None = None,
    spoiler: bool = False,
    data: bytes | None = None,
) -> DiscordAttachment:
    reader = None
    if data is not None:

        def reader() -> bytes:
            return data

    return DiscordAttachment(
        id=attachment_id,
        filename=filename,
        url=url,
        proxy_url=proxy_url,
        content_type=content_type,
        size=size,
        description=description,
        spoiler=spoiler,
        read=reader,
    )


async def _deliver(
    msg: DiscordInboundMessage,
    audio_cache_dir: Path = Path("data/media/discord"),
    process_inbound_audio: (
        Callable[[InboundAudioProcessingRequest], Awaitable[InboundAudioProcessingResult]] | None
    ) = None,
    **cfg_kwargs: Any,
) -> tuple[str, NewMessage, list[tuple[str, str, str | None]]]:
    delivered: list[tuple[str, NewMessage]] = []
    metadata: list[tuple[str, str, str | None]] = []
    channel = DiscordChannel(
        "discord",
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, **cfg_kwargs),
        "token",
        lambda jid, new_message: delivered.append((jid, new_message)),
        lambda jid, timestamp, chat_name: metadata.append((jid, timestamp, chat_name)),
        audio_cache_dir=audio_cache_dir,
        process_inbound_audio=process_inbound_audio,
    )
    channel.bot_user_id = BOT_ID

    await channel.events.handle_inbound_message(msg)

    assert len(delivered) == 1
    jid, new_message = delivered[0]
    return jid, new_message, metadata


async def test_dm_message_keys_chat_off_sender_user_id():
    jid, msg, metadata = await _deliver(
        _message(
            author=_user("5", display_name="Alice"),
            guild_id=None,
            channel_id="dm1",
            content="hello",
        ),
        dm_policy="open",
    )

    assert jid == "discord:direct:5"
    assert msg.chat_jid == jid
    assert msg.sender == "5"
    assert msg.sender_name == "Alice"
    assert msg.content == "hello"
    assert metadata[0][0] == jid
    assert metadata[0][2] == "Alice"


async def test_guild_message_keys_chat_off_channel_id():
    jid, msg, metadata = await _deliver(
        _message(
            author=_user("5", display_name="Alice"),
            guild_id="g1",
            channel_id="c1",
            channel_name="code-improver",
            mentions=(BOT_ID,),
            content="hello",
        ),
        group_policy="open",
    )

    assert jid == "discord:channel:c1"
    assert msg.chat_jid == jid
    assert msg.metadata["discord_channel_name"] == "code-improver"
    assert metadata[0][2] == "code-improver"


async def test_thread_message_uses_thread_jid_and_parent_metadata():
    jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="t1",
            channel_name="run-123",
            parent_id="c1",
            parent_name="admin",
            content="thread update",
        ),
        group_policy="allowlist",
        chat={"g1": {"channels": {"c1": {"require_mention": False}}}},
    )

    assert jid == "discord:channel:t1"
    assert msg.metadata["discord_message_id"] == "m1"
    assert msg.metadata["discord_parent_chat_jid"] == "discord:channel:c1"
    assert msg.metadata["discord_channel_name"] == "run-123"
    assert msg.metadata["discord_parent_channel_name"] == "admin"


async def test_thread_created_system_message_is_not_delivered_to_parent_channel_agent():
    delivered: list[tuple[str, NewMessage]] = []
    metadata: list[tuple[str, str, str | None]] = []
    channel = DiscordChannel(
        "discord",
        DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"g1": {"channels": {"c1": {"require_mention": False}}}},
        ),
        "token",
        lambda jid, new_message: delivered.append((jid, new_message)),
        lambda jid, timestamp, chat_name: metadata.append((jid, timestamp, chat_name)),
        audio_cache_dir=Path("data/media/discord"),
    )
    channel.bot_user_id = BOT_ID

    await channel.events.handle_inbound_message(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            channel_name="admin",
            content="started a thread",
            message_type="thread_created",
        )
    )

    assert delivered == []
    assert metadata == []


async def test_application_command_becomes_an_intent_message_without_phrase_matching():
    delivered: list[tuple[str, NewMessage]] = []
    responses: list[tuple[str, bool]] = []
    channel = DiscordChannel(
        "discord",
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, group_policy="open"),
        "token",
        lambda jid, new_message: delivered.append((jid, new_message)),
        lambda _jid, _timestamp, _chat_name: None,
        audio_cache_dir=Path("data/media/discord"),
    )
    channel.bot_user_id = BOT_ID
    interaction = _ApplicationInteraction(
        id=42,
        user=_ApplicationUser(
            id="5",
            bot=False,
            display_name="Alice",
            global_name="Alice",
            name="alice",
            roles=[],
        ),
        guild=_ApplicationGuild(id="g1", name="Pynchy"),
        channel=_ApplicationChannel(id="c1", name="general", parent=None, parent_id=None),
        created_at=None,
        response=_ApplicationResponse(send_message=AsyncMock()),
    )

    def record_response(content: str, *, ephemeral: bool) -> None:
        responses.append((content, ephemeral))

    interaction.response.send_message.side_effect = record_response

    await channel.events.handle_application_command(interaction, "reset")

    assert len(delivered) == 1
    jid, message = delivered[0]
    assert jid == "discord:channel:c1"
    assert message.content == "/reset"
    assert message.metadata == {
        "discord_interaction_id": "42",
        "discord_channel_name": "general",
        "application_commands": True,
        "application_command": {"name": "reset", "options": {}},
    }
    assert responses == [("✅ /reset received", True)]


def test_discord_registers_native_application_commands():
    channel = DiscordChannel(
        "discord",
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
        "token",
        lambda _jid, _message: None,
        lambda _jid, _timestamp, _chat_name: None,
        audio_cache_dir=Path("data/media/discord"),
    )
    client = discord.Client(intents=discord.Intents.none())
    channel.client = client

    channel.events.register()

    tree = client._connection._command_tree
    assert tree is not None
    commands = {command.name: command.to_dict(tree) for command in tree.get_commands()}
    assert set(commands) == {
        "approve",
        "deny",
        "end-session",
        "pause",
        "pending",
        "redeploy",
        "reset",
    }
    assert commands["approve"]["options"][0]["name"] == "short_id"


async def test_reply_context_is_preserved_in_message_metadata():
    reference = DiscordReply(message_id="reply-1", sender_name="Alice", content="Original message")
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            content="Following up",
            reference=reference,
            mentions=(BOT_ID,),
        ),
        group_policy="open",
    )

    assert msg.metadata["discord_message_id"] == "m1"
    assert msg.metadata["reply_to_message_id"] == "reply-1"
    assert msg.metadata["reply_to_sender"] == "Alice"
    assert msg.metadata["reply_to_text"] == "Original message"


async def test_attachments_are_preserved_in_message_metadata():
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            content="See attached",
            attachments=(
                _attachment(
                    attachment_id="a1",
                    filename="design.txt",
                    description="Architecture sketch",
                ),
            ),
            mentions=(BOT_ID,),
        ),
        group_policy="open",
    )

    assert msg.metadata["attachments"] == [
        {
            "id": "a1",
            "filename": "design.txt",
            "url": "https://example.invalid/file.txt",
            "proxy_url": "https://cdn.example.invalid/file.txt",
            "content_type": "text/plain",
            "size": 12,
            "description": "Architecture sketch",
            "spoiler": False,
        }
    ]


async def test_audio_only_attachment_gets_placeholder_content():
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            attachments=(
                _attachment(
                    attachment_id="a1",
                    filename="voice.ogg",
                    content_type="audio/ogg",
                ),
            ),
            mentions=(BOT_ID,),
        ),
        group_policy="open",
    )

    assert msg.content == (
        "[Audio attachment received; transcription is not available yet: voice.ogg]"
    )
    assert msg.metadata["attachments"][0]["content_type"] == "audio/ogg"


async def test_audio_attachment_is_cached_and_transcribed(tmp_path: Path):
    def transcribe(path: Path) -> AudioTranscriptionResult:
        assert path.read_bytes() == b"voice bytes"
        return AudioTranscriptionResult(
            success=True,
            transcript="stretch first, then breathe",
            provider="local",
            model="base",
            error=None,
        )

    with patch(
        "pynchy.host.audio.transcribe_audio_file",
        new=AsyncMock(side_effect=transcribe),
        create=True,
    ):
        _jid, msg, _metadata = await _deliver(
            _message(
                author=_user("5"),
                guild_id="g1",
                channel_id="c1",
                attachments=(
                    _attachment(
                        attachment_id="a1",
                        filename="voice.ogg",
                        content_type="audio/ogg",
                        data=b"voice bytes",
                    ),
                ),
                mentions=(BOT_ID,),
            ),
            audio_cache_dir=tmp_path,
            process_inbound_audio=process_inbound_audio_attachments,
            group_policy="open",
        )

    assert msg.content == (
        '[The user sent a voice message~ Here\'s what they said: "stretch first, then breathe"]'
    )
    attachment = msg.metadata["attachments"][0]
    assert attachment["cached_path"] == str(tmp_path / "m1-a1.ogg")
    assert attachment["transcription"] == {
        "success": True,
        "provider": "local",
        "model": "base",
    }


async def test_forwarded_snapshot_text_falls_back_when_message_content_missing():
    snapshot = DiscordForwardedMessage(
        type_name="default",
        content="Forwarded content",
        created_at=datetime(2026, 7, 7, tzinfo=UTC),
        attachments=(
            _attachment(
                attachment_id="forward-1",
                filename="trace.json",
                url="https://example.invalid/trace.json",
                proxy_url="https://cdn.example.invalid/trace.json",
                content_type="application/json",
                size=64,
            ),
        ),
    )
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            message_snapshots=(snapshot,),
            mentions=(BOT_ID,),
        ),
        group_policy="open",
    )

    assert msg.content == "Forwarded content"
    assert msg.metadata["forwarded_messages"] == [
        {
            "content": "Forwarded content",
            "created_at": "2026-07-07T00:00:00+00:00",
            "type": "default",
            "attachments": [
                {
                    "id": "forward-1",
                    "filename": "trace.json",
                    "url": "https://example.invalid/trace.json",
                    "proxy_url": "https://cdn.example.invalid/trace.json",
                    "content_type": "application/json",
                    "size": 64,
                    "description": None,
                    "spoiler": False,
                }
            ],
        }
    ]
