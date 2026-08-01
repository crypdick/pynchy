"""Public Discord voice-manager fallback behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import discord
import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.api import AudioTranscriptionResult
from pynchy.plugins.channels.discord import DiscordChannel
from pynchy.plugins.speech.api import SpeechSynthesisResult
from tests.discord_channel_support import (
    DISCORD_BOT_ENV,
    DISCORD_BOT_VALUE,
    _activate_voice_session,
    _configured_voice_channel,
    _FakeDiscordUser,
    _FakeVoiceChannel,
    _VoiceState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pynchy.workspace.api import WorkspaceProfile


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
async def test_voice_room_with_an_allowed_member_activates() -> None:
    channel = _configured_voice_channel()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), asyncio.Event())
    voice_channel.release.set()
    voice_channel.members = [_FakeDiscordUser(42, "Alice")]

    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    assert voice_channel.connect_calls == 1


@pytest.mark.asyncio
async def test_voice_on_ready_rejoins_an_occupied_configured_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _configured_voice_channel()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), asyncio.Event())
    voice_channel.release.set()
    find_channel = AsyncMock(return_value=voice_channel)
    monkeypatch.setattr(channel, "find_configured_channel", find_channel)
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
        lambda _manager, _channel: {"42": "Alice"},
    )
    monkeypatch.setattr("pynchy.plugins.channels.discord._voice._load_opus", lambda: True)

    await channel.voice.on_ready()

    find_channel.assert_awaited_once()
    assert voice_channel.connect_calls == 1


@pytest.mark.asyncio
async def test_voice_on_ready_ignores_a_missing_configured_room() -> None:
    channel = _configured_voice_channel()
    channel.find_configured_channel = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await channel.voice.on_ready()

    channel.find_configured_channel.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_on_ready_ignores_non_voice_configurations() -> None:
    channel = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            chat={"1": {"channels": {"2": {"kind": "text"}}}},
        ).to_runtime_settings(),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, _message: None,
        on_chat_metadata=lambda _jid, _timestamp, _name: None,
        audio_cache_dir=Path("data/media/discord"),
    )
    channel.find_configured_channel = AsyncMock()  # type: ignore[method-assign]

    await channel.voice.on_ready()

    channel.find_configured_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfigured_voice_room_is_ignored() -> None:
    channel = _configured_voice_channel()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), asyncio.Event())
    voice_channel.id = 999
    voice_channel.name = "Other"

    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    assert voice_channel.connect_calls == 0


@pytest.mark.asyncio
async def test_voice_room_from_a_different_guild_is_ignored() -> None:
    channel = _configured_voice_channel()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), asyncio.Event())
    voice_channel.guild.id = 999
    voice_channel.guild.name = "Unconfigured guild"

    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    assert voice_channel.connect_calls == 0


@pytest.mark.asyncio
async def test_text_only_configuration_does_not_activate_a_voice_room() -> None:
    channel = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            chat={"1": {"channels": {"2": {"name": "General", "kind": "text"}}}},
        ).to_runtime_settings(),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, _message: None,
        on_chat_metadata=lambda _jid, _timestamp, _name: None,
        audio_cache_dir=Path("data/media/discord"),
    )
    voice_channel = _FakeVoiceChannel(asyncio.Event(), asyncio.Event())

    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    assert voice_channel.connect_calls == 0


@pytest.mark.asyncio
async def test_voice_connection_failure_does_not_activate_session() -> None:
    channel = _configured_voice_channel()
    voice_channel = _FakeVoiceChannel(asyncio.Event(), asyncio.Event())
    voice_channel.connect = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.DiscordException("gateway unavailable")
    )
    with patch.object(type(channel.voice), "_allowed_members", return_value={"42": "Alice"}):
        await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))
    await channel.voice.speak("discord:voice:2", "hello")

    voice_channel.connect.assert_awaited_once()


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
async def test_voice_reply_without_synthesis_provider_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _configured_voice_channel()
    voice_client = await _activate_voice_session(channel, monkeypatch)

    await channel.voice.speak("discord:voice:2", "hello")

    assert voice_client.played_audio == []


@pytest.mark.asyncio
async def test_voice_disconnect_skips_already_disconnected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _configured_voice_channel()
    voice_client = await _activate_voice_session(channel, monkeypatch)
    voice_client.is_connected = lambda: False  # type: ignore[method-assign]
    voice_client.disconnect = AsyncMock()  # type: ignore[method-assign]

    await channel.voice.disconnect()

    voice_client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_reply_skips_failed_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingSynthesizer:
        async def synthesize(self, _text: str, _output_path) -> SpeechSynthesisResult:
            return SpeechSynthesisResult(success=False, error="synthesis unavailable")

    channel = _configured_voice_channel(FailingSynthesizer())
    voice_client = await _activate_voice_session(channel, monkeypatch)

    await channel.voice.speak("discord:voice:2", "hello")

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
        await channel.voice.speak("discord:voice:2", "hello")

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
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.discord.FFmpegOpusAudio", lambda _path: object()
    )

    with patch.object(voice_client, "play", side_effect=discord.DiscordException("offline")):
        await channel.voice.speak("discord:voice:2", "hello")

    assert voice_client.played_audio == []


@pytest.mark.asyncio
async def test_voice_reply_tolerates_playback_completion_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Synthesizer:
        async def synthesize(self, _text: str, output_path) -> SpeechSynthesisResult:
            return SpeechSynthesisResult(success=True, output_path=output_path)

    channel = _configured_voice_channel(Synthesizer())
    voice_client = await _activate_voice_session(channel, monkeypatch)
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.discord.FFmpegOpusAudio", lambda _path: object()
    )

    def play_with_error(_audio: object, *, after) -> None:
        after(RuntimeError("playback failed"))
        after(RuntimeError("duplicate callback"))

    with patch.object(voice_client, "play", side_effect=play_with_error):
        await channel.voice.speak("discord:voice:2", "hello")

    assert voice_client.played_audio == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transcription", "expected_messages"),
    [
        (AudioTranscriptionResult(success=False, error="transcription unavailable"), []),
        (AudioTranscriptionResult(success=True, transcript="hello", provider="test"), ["hello"]),
    ],
)
async def test_voice_input_transcription_is_forwarded_only_when_successful(
    monkeypatch: pytest.MonkeyPatch,
    transcription: AudioTranscriptionResult,
    expected_messages: list[str],
) -> None:
    messages = []
    observed_headers: list[bytes] = []
    transcription_finished = asyncio.Event()

    async def transcribe(path: Path) -> AudioTranscriptionResult:
        observed_headers.append((await asyncio.to_thread(path.read_bytes))[:4])
        transcription_finished.set()
        return transcription

    channel = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            chat={"1": {"name": "Pynchy", "channels": {"2": {"kind": "voice"}}}},
        ).to_runtime_settings(),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, message: messages.append(message),
        on_chat_metadata=lambda _jid, _timestamp, _name: None,
        audio_cache_dir=Path("data/media/discord"),
        workspaces=lambda: {"discord:voice:2": cast("WorkspaceProfile", object())},
        transcribe_audio=transcribe,
    )
    voice_channel = _FakeVoiceChannel(asyncio.Event(), asyncio.Event())
    voice_channel.release.set()
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
        lambda _manager, _channel: {"42": "Alice"},
    )
    monkeypatch.setattr("pynchy.plugins.channels.discord._voice._load_opus", lambda: True)

    class Decoder:
        calls = 0

        def decode(self, _packet: bytes, **_kwargs: bool) -> bytes:
            self.calls += 1
            return b"\x00\x01" * 100 if self.calls == 1 else b"\x00\x00" * 100

    monkeypatch.setattr("pynchy.plugins.channels.discord._voice._new_opus_decoder", Decoder)
    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))
    listener = cast("Callable[[str, bytes], None]", voice_channel.voice_client.received_listener)
    listener("42", b"speech")

    for _ in range(30):
        listener("42", b"silence")
    await asyncio.wait_for(transcription_finished.wait(), timeout=1)

    assert [message.content for message in messages] == expected_messages
    assert observed_headers == [b"RIFF"]
