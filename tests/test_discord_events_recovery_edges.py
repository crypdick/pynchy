"""Reachable Discord inbound recovery behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import discord
import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.api import AudioMetadataPatch, InboundAudioProcessingResult
from pynchy.plugins.channels.discord import DiscordAttachment, DiscordChannel, DiscordReply
from tests.test_discord_events import (
    BOT_ID,
    DISCORD_BOT_ENV,
    _deliver,
    _message,
    _user,
)


async def test_audio_processing_accepts_async_attachment_reads_and_patches_metadata(
    tmp_path: Path,
):
    async def read_audio() -> bytes:
        await asyncio.sleep(0)
        return b"async voice bytes"

    async def process(request: Any) -> InboundAudioProcessingResult:
        await asyncio.sleep(0)
        assert request.attachments[0].data == b"async voice bytes"
        return InboundAudioProcessingResult(
            content="transcribed async voice",
            metadata_patches=(
                AudioMetadataPatch(
                    index=0,
                    cached_path=str(tmp_path / "voice.ogg"),
                    transcription={"success": True},
                ),
            ),
        )

    audio = DiscordAttachment(
        id="a1",
        filename="voice.ogg",
        url="https://example.invalid/voice.ogg",
        proxy_url="https://cdn.example.invalid/voice.ogg",
        content_type="audio/ogg",
        size=17,
        description=None,
        spoiler=False,
        read=read_audio,
    )
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            attachments=(audio,),
            mentions=(BOT_ID,),
        ),
        audio_cache_dir=tmp_path,
        process_inbound_audio=process,
        group_policy="open",
    )

    assert msg.content == "transcribed async voice"
    assert msg.metadata["attachments"][0]["cached_path"] == str(tmp_path / "voice.ogg")
    assert msg.metadata["attachments"][0]["transcription"] == {"success": True}


async def test_audio_processing_ignores_patches_for_unknown_attachments():
    async def process(_request: Any) -> InboundAudioProcessingResult:
        await asyncio.sleep(0)
        return InboundAudioProcessingResult(
            content="processed",
            metadata_patches=(
                AudioMetadataPatch(index=1, cached_path="ignored", transcription={"ok": True}),
            ),
        )

    audio = DiscordAttachment(
        id="a1",
        filename="voice.ogg",
        url="https://example.invalid/voice.ogg",
        proxy_url="https://cdn.example.invalid/voice.ogg",
        content_type="audio/ogg",
        size=17,
        description=None,
        spoiler=False,
        read=lambda: b"voice",
    )
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            attachments=(audio,),
            mentions=(BOT_ID,),
        ),
        process_inbound_audio=process,
        group_policy="open",
    )

    assert msg.content == "processed"
    assert "cached_path" not in msg.metadata["attachments"][0]


@pytest.mark.asyncio
async def test_application_command_sync_is_one_shot_after_success():
    channel = DiscordChannel(
        "discord",
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
        "token",
        lambda _jid, _message: None,
        lambda _jid, _timestamp, _chat_name: None,
        audio_cache_dir=Path("data/media/discord"),
    )
    channel.client = discord.Client(intents=discord.Intents.none())
    channel.events.register()

    with patch.object(
        discord.app_commands.CommandTree,
        "sync",
        new=AsyncMock(return_value=[object(), object()]),
    ) as sync:
        await channel.events.sync_application_commands()
        await channel.events.sync_application_commands()

    sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_application_command_sync_retries_after_discord_failure():
    channel = DiscordChannel(
        "discord",
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
        "token",
        lambda _jid, _message: None,
        lambda _jid, _timestamp, _chat_name: None,
        audio_cache_dir=Path("data/media/discord"),
    )
    channel.client = discord.Client(intents=discord.Intents.none())
    channel.events.register()

    with patch.object(
        discord.app_commands.CommandTree,
        "sync",
        new=AsyncMock(side_effect=[discord.DiscordException("offline"), []]),
    ) as sync:
        await channel.events.sync_application_commands()
        await channel.events.sync_application_commands()

    assert sync.await_count == 2


async def test_audio_read_failure_keeps_the_message_deliverable():
    def read_audio() -> bytes:
        raise OSError("attachment disappeared")

    audio = DiscordAttachment(
        id="a1",
        filename="voice.ogg",
        url="https://example.invalid/voice.ogg",
        proxy_url="https://cdn.example.invalid/voice.ogg",
        content_type="audio/ogg",
        size=17,
        description=None,
        spoiler=False,
        read=read_audio,
    )
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            attachments=(audio,),
            mentions=(BOT_ID,),
        ),
        group_policy="open",
    )

    assert msg.content == (
        "[Audio attachment received; transcription is not available yet: voice.ogg]"
    )


async def test_duplicate_and_self_messages_are_not_delivered():
    delivered: list[Any] = []
    channel = DiscordChannel(
        "discord",
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, group_policy="open"),
        "token",
        lambda _jid, message: delivered.append(message),
        lambda _jid, _timestamp, _chat_name: None,
        audio_cache_dir=Path("data/media/discord"),
    )
    channel.bot_user_id = BOT_ID
    message = _message(
        author=_user("5"),
        guild_id="g1",
        channel_id="c1",
        content="hello",
        mentions=(BOT_ID,),
    )

    await channel.events.handle_inbound_message(message)
    await channel.events.handle_inbound_message(message)
    await channel.events.handle_inbound_message(
        _message(
            author=_user(BOT_ID),
            guild_id="g1",
            channel_id="c1",
            content="self",
        )
    )

    assert [item.content for item in delivered] == ["hello"]


async def test_empty_discord_message_has_empty_content_without_fallbacks():
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            mentions=(BOT_ID,),
        ),
        group_policy="open",
    )

    assert not msg.content


async def test_unresolved_reply_does_not_add_empty_reply_metadata():
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            content="follow-up",
            reference=DiscordReply(message_id=None, sender_name=None, content=""),
            mentions=(BOT_ID,),
        ),
        group_policy="open",
    )

    assert msg.metadata is not None
    assert not any(key.startswith("reply_to_") for key in msg.metadata)


def test_registers_gateway_handlers_without_native_command_support():
    class EventClient:
        def __init__(self) -> None:
            self.handlers: dict[str, object] = {}

        def event(self, handler: object) -> object:
            self.handlers[handler.__name__] = handler
            return handler

    channel = DiscordChannel(
        "discord",
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
        "token",
        lambda _jid, _message: None,
        lambda _jid, _timestamp, _chat_name: None,
        audio_cache_dir=Path("data/media/discord"),
    )
    client = EventClient()
    channel.client = client

    channel.events.register()

    assert set(client.handlers) == {"on_message", "on_raw_reaction_add"}
