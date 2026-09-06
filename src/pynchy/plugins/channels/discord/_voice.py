"""One configured Discord voice workspace and its bounded spoken turns."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import wave
from collections.abc import (
    Awaitable,
    Callable,
)
from ctypes.util import find_library
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

import discord
from discord import opus

from pynchy.async_tasks import create_background_task
from pynchy.discord import DiscordChatTarget
from pynchy.logger import logger
from pynchy.plugins.api import (
    AudioTranscriptionResult,
    NewMessage,
)

from ._access import InboundContext
from ._ids import voice_jid
from ._lookup import discord_user_names, same_name
from ._voice_client import PynchyVoiceClient

if TYPE_CHECKING:
    from pynchy.plugins.speech.api import SpeechSynthesisProvider

    from ._channel import DiscordChannel
else:
    DiscordChannel = object
    SpeechSynthesisProvider = object

_PCM_SAMPLE_RATE = 48_000
_PCM_CHANNELS = 2
_PCM_SAMPLE_WIDTH = 2
_SILENCE_FRAMES = 30
_SPEECH_RMS_THRESHOLD = 250
_MAX_TURN_BYTES = _PCM_SAMPLE_RATE * _PCM_CHANNELS * _PCM_SAMPLE_WIDTH * 30
_OPUS_LIBRARY_ENV = "PYNCHY_DISCORD_OPUS_LIBRARY"
_OPUS_LIBRARY_CANDIDATES = (
    "/opt/homebrew/opt/opus/lib/libopus.0.dylib",
    "/usr/local/opt/opus/lib/libopus.0.dylib",
)


class _OpusDecoder(Protocol):
    def decode(self, data: bytes, **_kwargs: bool) -> bytes: ...


# discord.py ships no type information for its Opus decoder.
_new_opus_decoder = cast("Callable[[], _OpusDecoder]", opus.Decoder)


def _load_opus() -> bool:
    """Load libopus from the platform resolver or the supported macOS installs."""
    if opus.is_loaded():
        return True
    candidates = (
        os.getenv(_OPUS_LIBRARY_ENV),
        find_library("opus"),
        *_OPUS_LIBRARY_CANDIDATES,
    )
    for candidate in dict.fromkeys(candidate for candidate in candidates if candidate):
        try:
            opus.load_opus(candidate)
        except OSError:
            continue
        logger.info("Loaded libopus for Discord voice", library=candidate)
        return True
    return False


@dataclass
class _TurnBuffer:
    pcm: bytearray = field(default_factory=bytearray)
    silence_frames: int = 0

    def add(self, frame: bytes) -> bytes | None:
        speech = _pcm_rms(frame) >= _SPEECH_RMS_THRESHOLD
        if not self.pcm and not speech:
            return None
        self.pcm.extend(frame)
        self.silence_frames = 0 if speech else self.silence_frames + 1
        if self.silence_frames < _SILENCE_FRAMES and len(self.pcm) < _MAX_TURN_BYTES:
            return None
        completed = bytes(self.pcm)
        self.pcm.clear()
        self.silence_frames = 0
        return completed


def _pcm_rms(frame: bytes) -> int:
    if not frame:
        return 0
    samples = memoryview(frame).cast("h")
    return int((sum(sample * sample for sample in samples) / len(samples)) ** 0.5)


class DiscordVoiceManager:
    """Join the single configured voice workspace while an allowed user is present."""

    def __init__(
        self,
        channel: DiscordChannel,
        speech_synthesizer: SpeechSynthesisProvider | None = None,
        transcribe_audio: Callable[[Path], Awaitable[AudioTranscriptionResult]] | None = None,
    ) -> None:
        self._channel = channel
        self._speech_synthesizer = speech_synthesizer
        self._transcribe_audio = transcribe_audio
        self._session: _VoiceSession | None = None
        self._refresh_lock = asyncio.Lock()

    async def on_ready(self) -> None:
        """Join an already occupied configured room after a gateway reconnect."""
        for guild_key, guild in self._channel.config.chat.items():
            for channel_key, config in guild.channels.items():
                if config.kind != "voice":
                    continue
                target = DiscordChatTarget(
                    kind="channel",
                    guild_id=guild_key,
                    target_id=channel_key,
                )
                voice_channel = await self._channel.find_configured_channel(target)
                if voice_channel is not None:
                    await self._refresh(voice_channel)

    async def on_voice_state_update(self, _member: object, before: object, after: object) -> None:
        """Refresh the configured room when any member joins, leaves, or moves."""
        for state in (before, after):
            voice_channel = getattr(state, "channel", None)
            if voice_channel is not None and self._is_configured_voice_channel(voice_channel):
                await self._refresh(voice_channel)
                return

    async def speak(self, jid: str, text: str) -> None:
        session = self._session
        if session is None or session.jid != jid:
            logger.warning("Skipped Discord voice reply; no active voice session", jid=jid)
            return
        await session.speak(text)

    async def disconnect(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    async def _refresh(self, voice_channel: object) -> None:
        """Apply one voice-state update after any in-progress connection finishes."""
        async with self._refresh_lock:
            await self._refresh_locked(voice_channel)

    async def _refresh_locked(self, voice_channel: object) -> None:
        jid = voice_jid(str(getattr(voice_channel, "id", "")))
        if not self._workspace_exists(jid):
            return
        members = self._allowed_members(voice_channel)
        if not members:
            if self._session is not None and self._session.jid == jid:
                await self.disconnect()
            return
        if self._session is not None:
            if self._session.jid != jid:
                logger.warning(
                    "Discord voice connection already belongs to another configured room"
                )
                return
            self._session.update_members(members)
            return

        try:
            voice_client = await cast("Any", voice_channel).connect(
                cls=PynchyVoiceClient,
                reconnect=True,
                self_deaf=False,
            )
        except discord.DiscordException as exc:
            logger.warning("Discord voice connection failed", jid=jid, err=str(exc))
            return
        session = _VoiceSession(
            self._channel,
            jid,
            cast("PynchyVoiceClient", voice_client),
            members,
            self._speech_synthesizer,
            self._transcribe_audio,
        )
        self._session = session
        session.start()
        logger.info("Joined configured Discord voice workspace", jid=jid)

    def _workspace_exists(self, jid: str) -> bool:
        workspaces = self._channel.workspaces() if self._channel.workspaces is not None else {}
        return jid in workspaces

    def _is_configured_voice_channel(self, voice_channel: object) -> bool:
        guild = getattr(voice_channel, "guild", None)
        guild_id = str(getattr(guild, "id", ""))
        guild_name = getattr(guild, "name", None)
        channel_id = str(getattr(voice_channel, "id", ""))
        channel_name = getattr(voice_channel, "name", None)
        for guild_key, guild_config in self._channel.config.chat.items():
            if guild_key != guild_id and not same_name(guild_config.name or guild_key, guild_name):
                continue
            for channel_key, channel_config in guild_config.channels.items():
                if channel_config.kind != "voice":
                    continue
                configured_name = channel_config.name or channel_key
                if channel_key == channel_id or same_name(configured_name, channel_name):
                    return True
        return False

    def _allowed_members(self, voice_channel: object) -> dict[str, str]:
        guild = getattr(voice_channel, "guild", None)
        allowed: dict[str, str] = {}
        for member in getattr(voice_channel, "members", ()):
            member_id = str(getattr(member, "id", ""))
            roles = frozenset(str(getattr(role, "id", "")) for role in getattr(member, "roles", ()))
            ctx = InboundContext(
                is_dm=False,
                author_id=member_id,
                author_is_bot=bool(getattr(member, "bot", False)),
                guild_id=str(getattr(guild, "id", "")) or None,
                guild_name=getattr(guild, "name", None),
                channel_id=str(getattr(voice_channel, "id", "")),
                channel_name=getattr(voice_channel, "name", None),
                parent_channel_id=None,
                parent_channel_name=None,
                author_names=frozenset(discord_user_names(member)),
                author_role_ids=roles,
                mentions_bot=False,
                is_voice=True,
            )
            if self._channel.access.decide(ctx) == "allow":
                allowed[member_id] = str(getattr(member, "display_name", member_id))
        return allowed


class _VoiceSession:
    def __init__(  # noqa: PLR0913 - one session owns its channel and optional providers.
        self,
        channel: DiscordChannel,
        jid: str,
        voice_client: PynchyVoiceClient,
        allowed_members: dict[str, str],
        speech_synthesizer: SpeechSynthesisProvider | None,
        transcribe_audio: Callable[[Path], Awaitable[AudioTranscriptionResult]] | None,
    ) -> None:
        self.jid = jid
        self._channel = channel
        self._voice_client = voice_client
        self._allowed_members = allowed_members
        self._speech_synthesizer = speech_synthesizer
        self._transcribe_audio = transcribe_audio
        self._decoders: dict[str, _OpusDecoder] = {}
        self._turns: dict[str, _TurnBuffer] = {}
        self._speaking_lock = asyncio.Lock()
        self._receiving = False

    def start(self) -> None:
        if not _load_opus():
            logger.warning(
                "Discord voice input disabled; libopus is not available",
                jid=self.jid,
                env_var=_OPUS_LIBRARY_ENV,
            )
            return
        self._voice_client.start_receiving(self._on_packet)
        self._receiving = True

    def update_members(self, allowed_members: dict[str, str]) -> None:
        self._allowed_members = allowed_members
        self._decoders = {
            user_id: decoder
            for user_id, decoder in self._decoders.items()
            if user_id in allowed_members
        }
        self._turns = {
            user_id: turn for user_id, turn in self._turns.items() if user_id in allowed_members
        }

    async def speak(self, text: str) -> None:
        if not self._voice_client.is_connected():
            logger.warning("Skipped Discord voice reply; client disconnected", jid=self.jid)
            return
        if self._speech_synthesizer is None:
            logger.warning("Skipped Discord voice reply; no synthesis provider", jid=self.jid)
            return
        async with self._speaking_lock:
            with tempfile.TemporaryDirectory(prefix="pynchy-discord-tts-") as temp_dir:
                output_path = Path(temp_dir) / "reply.wav"
                result = await self._speech_synthesizer.synthesize(text, output_path)
                if not result.success or result.output_path is None:
                    logger.warning(
                        "Discord voice synthesis failed",
                        jid=self.jid,
                        error=result.error,
                    )
                    return
                await self._play_audio(result.output_path)

    async def close(self) -> None:
        if self._receiving:
            self._voice_client.stop_receiving()
            self._receiving = False
        self._voice_client.stop()
        if self._voice_client.is_connected():
            with contextlib.suppress(discord.DiscordException):
                await self._voice_client.disconnect()

    def _on_packet(self, speaker_id: str, opus_packet: bytes) -> None:
        if speaker_id not in self._allowed_members:
            return
        decoder = self._decoders.setdefault(speaker_id, _new_opus_decoder())
        try:
            pcm = decoder.decode(opus_packet, fec=False)
        except opus.OpusError as exc:
            logger.debug("Discarded invalid Discord voice Opus packet", err=str(exc))
            return
        completed_turn = self._turns.setdefault(speaker_id, _TurnBuffer()).add(pcm)
        if completed_turn is not None:
            create_background_task(
                self._transcribe_turn(speaker_id, completed_turn),
                name=f"discord-voice-stt-{self.jid[-18:]}",
            )

    async def _transcribe_turn(self, speaker_id: str, pcm: bytes) -> None:
        transcribe_audio = self._transcribe_audio
        if transcribe_audio is None:
            logger.warning("Skipped Discord voice transcription; no host transcriber is configured")
            return
        with tempfile.TemporaryDirectory(prefix="pynchy-discord-stt-") as temp_dir:
            audio_path = Path(temp_dir) / "turn.wav"
            _write_wave(audio_path, pcm)
            result = await transcribe_audio(audio_path)
        transcript = result.transcript.strip()
        if not result.success or not transcript:
            logger.warning("Discord voice transcription failed", jid=self.jid, error=result.error)
            return
        message = NewMessage(
            id=f"discord-voice-{uuid4().hex}",
            chat_jid=self.jid,
            sender=f"discord:{speaker_id}",
            sender_name=self._allowed_members.get(speaker_id, speaker_id),
            content=transcript,
            timestamp=datetime.now(UTC).isoformat(),
            metadata={"discord_voice": {"provider": result.provider, "model": result.model}},
        )
        self._channel.on_message(self.jid, message)

    async def _play_audio(self, audio_path: Path) -> None:
        completed: asyncio.Future[Exception | None] = asyncio.get_running_loop().create_future()

        def after(error: Exception | None) -> None:
            def complete() -> None:
                if not completed.done():
                    completed.set_result(error)

            completed.get_loop().call_soon_threadsafe(complete)

        try:
            self._voice_client.play(discord.FFmpegOpusAudio(str(audio_path)), after=after)
        except (OSError, discord.DiscordException) as exc:
            logger.warning("Discord voice playback failed to start", jid=self.jid, err=str(exc))
            return
        if error := await completed:
            logger.warning("Discord voice playback failed", jid=self.jid, err=str(error))


def _write_wave(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as audio_file:
        audio_file.setnchannels(_PCM_CHANNELS)
        audio_file.setsampwidth(_PCM_SAMPLE_WIDTH)
        audio_file.setframerate(_PCM_SAMPLE_RATE)
        audio_file.writeframes(pcm)
