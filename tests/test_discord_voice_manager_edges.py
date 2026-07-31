"""Public Discord voice-manager fallback behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import discord
import pytest

from pynchy.plugins.api import OutboundEvent, OutboundEventType
from pynchy.plugins.speech.api import SpeechSynthesisResult
from tests.discord_channel_support import (
    _activate_voice_session,
    _configured_voice_channel,
    _FakeDiscordUser,
    _FakeVoiceChannel,
    _VoiceState,
)


@pytest.mark.asyncio
async def test_voice_connection_stays_receive_disabled_without_opus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _configured_voice_channel()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), asyncio.Event())
    voice_channel.release.set()
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
        lambda _manager, _channel: {"42": "Alice"},
    )
    monkeypatch.delenv("PYNCHY_DISCORD_OPUS_LIBRARY", raising=False)

    with (
        patch("pynchy.plugins.channels.discord._voice.find_library", return_value=None),
        patch("pynchy.plugins.channels.discord._voice.opus.is_loaded", return_value=False),
        patch(
            "pynchy.plugins.channels.discord._voice.opus.load_opus", side_effect=OSError("missing")
        ),
    ):
        await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    assert voice_channel.voice_client.received_listener is None


@pytest.mark.asyncio
async def test_voice_room_with_only_bots_does_not_activate() -> None:
    channel = _configured_voice_channel()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), asyncio.Event())
    bot = _FakeDiscordUser(7, "bot")
    bot.bot = True
    voice_channel.members = [bot]

    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    assert voice_channel.connect_calls == 0


@pytest.mark.asyncio
async def test_voice_room_disconnects_when_allowed_members_leave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _configured_voice_channel()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), asyncio.Event())
    voice_channel.release.set()
    remaining_members = iter(({"42": "Alice"}, {}))
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
        lambda _manager, _channel: next(remaining_members),
    )
    monkeypatch.setattr("pynchy.plugins.channels.discord._voice._load_opus", lambda: True)

    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))
    with patch.object(voice_channel.voice_client, "disconnect", new=AsyncMock()) as disconnect:
        await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    assert voice_channel.connect_calls == 1
    assert voice_channel.voice_client.received_listener is None
    disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_reply_without_an_active_session_is_ignored() -> None:
    channel = _configured_voice_channel()

    await channel.voice.speak("discord:voice:2", "hello")


@pytest.mark.asyncio
async def test_voice_reply_skips_failed_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingSynthesizer:
        async def synthesize(self, _text: str, _output_path) -> SpeechSynthesisResult:
            return SpeechSynthesisResult(success=False, error="synthesis unavailable")

    channel = _configured_voice_channel(FailingSynthesizer())
    voice_client = await _activate_voice_session(channel, monkeypatch)

    await channel.send_event(
        "discord:voice:2",
        OutboundEvent(type=OutboundEventType.RESULT, content="hello"),
    )

    assert voice_client.played_audio == []


@pytest.mark.asyncio
async def test_voice_reply_tolerates_disconnected_playback_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Synthesizer:
        async def synthesize(self, _text: str, output_path) -> SpeechSynthesisResult:
            return SpeechSynthesisResult(success=True, output_path=output_path)

    channel = _configured_voice_channel(Synthesizer())
    voice_client = await _activate_voice_session(channel, monkeypatch)

    with patch.object(voice_client, "is_connected", return_value=False):
        await channel.send_event(
            "discord:voice:2",
            OutboundEvent(type=OutboundEventType.RESULT, content="hello"),
        )

    assert voice_client.played_audio == []


@pytest.mark.asyncio
async def test_voice_reply_tolerates_playback_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Synthesizer:
        async def synthesize(self, _text: str, output_path) -> SpeechSynthesisResult:
            return SpeechSynthesisResult(success=True, output_path=output_path)

    channel = _configured_voice_channel(Synthesizer())
    voice_client = await _activate_voice_session(channel, monkeypatch)

    with patch.object(voice_client, "play", side_effect=discord.DiscordException("offline")):
        await channel.send_event(
            "discord:voice:2",
            OutboundEvent(type=OutboundEventType.RESULT, content="hello"),
        )

    assert voice_client.played_audio == []
