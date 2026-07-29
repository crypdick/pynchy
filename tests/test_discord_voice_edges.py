"""Behavior-focused coverage for optional Discord voice paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import discord
import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.api import AudioTranscriptionResult, OutboundEvent, OutboundEventType
from pynchy.plugins.channels.discord import DiscordChannel
from pynchy.plugins.speech.api import SpeechSynthesisResult
from tests.discord_channel_support import (
    DISCORD_BOT_ENV,
    DISCORD_BOT_VALUE,
    _activate_voice_session,
    _channel,
    _configured_voice_channel,
    _FakeVoiceChannel,
    _VoiceState,
)

if TYPE_CHECKING:
    from pynchy.workspace.api import WorkspaceProfile


def _voice_config() -> DiscordConnectionConfig:
    return DiscordConnectionConfig(
        bot_token_env=DISCORD_BOT_ENV,
        chat={"1": {"name": "Pynchy", "channels": {"2": {"kind": "voice"}}}},
    )


@pytest.mark.asyncio
async def test_voice_state_skips_unregistered_workspace(monkeypatch):
    channel = _channel(config=_voice_config())
    release = asyncio.Event()
    release.set()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), release)
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
        lambda _manager, _voice_channel: {"42": "Alice"},
    )

    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    assert voice_channel.connect_calls == 0


@pytest.mark.asyncio
async def test_voice_state_ignores_discord_connection_failure(monkeypatch):
    channel = _configured_voice_channel()
    release = asyncio.Event()
    release.set()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), release)
    voice_channel.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.DiscordException("offline")
    )
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
        lambda _manager, _voice_channel: {"42": "Alice"},
    )

    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    assert voice_channel.connect.await_count == 1


@pytest.mark.asyncio
async def test_voice_session_stops_cleanly_and_suppresses_disconnect_failure(monkeypatch):
    channel = _configured_voice_channel()
    voice_client = await _activate_voice_session(channel, monkeypatch)
    voice_client.disconnect = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.DiscordException("already gone")
    )

    await channel.voice.disconnect()

    voice_client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_reply_skips_disconnected_client_before_synthesis(monkeypatch):
    calls: list[str] = []

    class Synthesizer:
        async def synthesize(self, text: str, _output_path: Path) -> SpeechSynthesisResult:
            calls.append(text)
            return SpeechSynthesisResult(success=True)

    channel = _configured_voice_channel(Synthesizer())
    voice_client = await _activate_voice_session(channel, monkeypatch)
    voice_client.is_connected = lambda: False  # type: ignore[method-assign]

    await channel.send_event(
        "discord:voice:2",
        OutboundEvent(type=OutboundEventType.RESULT, content="Hello"),
    )

    assert calls == []


@pytest.mark.asyncio
async def test_voice_reply_skips_failed_synthesis(monkeypatch):
    class Synthesizer:
        async def synthesize(self, _text: str, _output_path: Path) -> SpeechSynthesisResult:
            return SpeechSynthesisResult(success=False, error="provider unavailable")

    channel = _configured_voice_channel(Synthesizer())
    voice_client = await _activate_voice_session(channel, monkeypatch)

    await channel.send_event(
        "discord:voice:2",
        OutboundEvent(type=OutboundEventType.RESULT, content="Hello"),
    )

    assert voice_client.played_audio == []


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["start", "after"])
async def test_voice_reply_handles_playback_failures(monkeypatch, completion):
    class Synthesizer:
        async def synthesize(self, _text: str, output_path: Path) -> SpeechSynthesisResult:
            return SpeechSynthesisResult(success=True, output_path=output_path)

    channel = _configured_voice_channel(Synthesizer())
    voice_client = await _activate_voice_session(channel, monkeypatch)
    if completion == "start":
        voice_client.play = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            OSError("ffmpeg missing")
        )
    else:
        voice_client.play = lambda _audio, *, after: after(  # type: ignore[method-assign]
            RuntimeError("playback failed")
        )

    with patch("pynchy.plugins.channels.discord._voice.discord.FFmpegOpusAudio"):
        await channel.send_event(
            "discord:voice:2",
            OutboundEvent(type=OutboundEventType.RESULT, content="Hello"),
        )


@pytest.mark.asyncio
async def test_voice_input_transcribes_a_completed_turn_and_delivers_message(monkeypatch):
    received: list[tuple[str, object]] = []
    seen_paths: list[Path] = []
    delivered = asyncio.Event()

    def on_message(jid: str, message: object) -> None:
        received.append((jid, message))
        delivered.set()

    async def transcribe(path: Path) -> AudioTranscriptionResult:
        await asyncio.sleep(0)
        seen_paths.append(path)
        return AudioTranscriptionResult(
            success=True,
            transcript="  hello from voice  ",
            provider="test",
            model="tiny",
        )

    channel = DiscordChannel(
        connection_name="connection.discord.test",
        config=_voice_config(),
        bot_token=DISCORD_BOT_VALUE,
        on_message=on_message,
        on_chat_metadata=lambda _jid, _timestamp, _name: None,
        audio_cache_dir=Path("data/media/discord"),
        workspaces=lambda: {"discord:voice:2": cast("WorkspaceProfile", object())},
        transcribe_audio=transcribe,
    )
    release = asyncio.Event()
    release.set()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), release)
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
        lambda _manager, _voice_channel: {"42": "Alice"},
    )
    monkeypatch.setattr("pynchy.plugins.channels.discord._voice._load_opus", lambda: True)

    class Decoder:
        def decode(self, packet: bytes, **_kwargs: bool) -> bytes:
            return b"\xe8\x03" if packet == b"speech" else b"\x00\x00"

    monkeypatch.setattr("pynchy.plugins.channels.discord._voice._new_opus_decoder", Decoder)
    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    listener = voice_channel.voice_client.received_listener
    assert listener is not None
    cast("object", listener)("42", b"speech")
    for _ in range(30):
        cast("object", listener)("42", b"silence")
    await asyncio.wait_for(delivered.wait(), timeout=1)

    assert len(received) == 1
    assert received[0][0] == "discord:voice:2"
    assert received[0][1].content == "hello from voice"
    assert seen_paths[0].suffix == ".wav"
