"""Tests for DiscordChannel outbound behavior and protocol conformance.

The discord.py client is faked; these cover the channel's own logic (jid
ownership, chunked sending with safe mention defaults, reaction id handling,
history catch-up filtering) rather than the gateway glue in _lifecycle.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from pynchy.config.models import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel, DiscordChannelPlugin, PynchyVoiceClient
from pynchy.plugins.speech import SpeechSynthesisResult, SpeechSynthesizerHealth
from pynchy.state import init_test_database, store_chat_metadata
from pynchy.types import Channel, OutboundEvent, OutboundEventType, WorkspaceProfile

DISCORD_BOT_ENV = "X"
DISCORD_BOT_VALUE = "token"

if TYPE_CHECKING:
    from pathlib import Path


def _channel(
    speech_synthesizer: object | None = None,
    config: DiscordConnectionConfig | None = None,
) -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=config or DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
        speech_synthesizer=speech_synthesizer,
    )


def _configured_voice_channel(speech_synthesizer: object | None = None) -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            chat={
                "1": {
                    "name": "Pynchy",
                    "channels": {"2": {"name": "General", "kind": "voice"}},
                }
            },
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, _message: None,
        on_chat_metadata=lambda _jid, _timestamp, _name: None,
        workspaces=lambda: {"discord:voice:2": cast("WorkspaceProfile", object())},
        speech_synthesizer=speech_synthesizer,
    )


class _FakeSendChannel:
    def __init__(self) -> None:
        self.sends: list[tuple[str, dict]] = []

    async def send(self, content: str, **kwargs) -> None:
        self.sends.append((content, kwargs))


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits: list[tuple[str, dict]] = []

    async def edit(self, *, content: str, **kwargs) -> None:
        self.edits.append((content, kwargs))


class _FakeStreamChannel:
    """A channel whose ``send`` returns a message and that can fetch it back."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, dict]] = []
        self.messages: dict[int, _FakeMessage] = {}
        self._next_id = 100

    async def send(self, content: str, **kwargs) -> _FakeMessage:
        self._next_id += 1
        msg = _FakeMessage(self._next_id)
        self.messages[msg.id] = msg
        self.sends.append((content, kwargs))
        return msg

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        return self.messages[message_id]


class _FakeTypingChannel:
    def __init__(self) -> None:
        self.typing_calls = 0

    async def typing(self) -> None:
        self.typing_calls += 1


@dataclass(slots=True)
class _FakeThread:
    id: int
    name: str = ""
    parent_id: int | None = None
    archived: bool = False
    added_user_ids: list[int] = field(default_factory=list)

    async def add_user(self, user: discord.Object) -> None:
        self.added_user_ids.append(int(user.id))

    async def edit(self, *, archived: bool) -> None:
        self.archived = archived


class _FakeThreadParent:
    def __init__(self) -> None:
        self.id = 123
        self.guild = _FakeThreadGuild()
        self.thread_requests: list[tuple[str, object]] = []
        self.created_threads: list[_FakeThread] = []
        self.sent_messages: list[str] = []
        self.archived: list[_FakeThread] = []

    async def create_thread(self, *, name: str, **kwargs: object) -> _FakeThread:
        self.thread_requests.append((name, kwargs["type"]))
        thread = _FakeThread(id=456)
        self.created_threads.append(thread)
        return thread

    async def send(self, content: str) -> None:
        self.sent_messages.append(content)

    async def archived_threads(self, **_kwargs: object):
        for thread in self.archived:
            yield thread


class _FakeThreadGuild:
    def __init__(self) -> None:
        self.threads: list[_FakeThread] = []

    async def active_threads(self) -> list[_FakeThread]:
        return self.threads


class _FakePynchyVoiceClient(PynchyVoiceClient):
    def __init__(self) -> None:
        self.received_listener: object | None = None
        self.played_audio: list[object] = []

    def is_connected(self) -> bool:
        return True

    def start_receiving(self, listener: object) -> None:
        self.received_listener = listener

    def play(self, audio: object, *, after) -> None:
        self.played_audio.append(audio)
        after(None)

    def stop(self) -> None:
        pass


class _FakeVoiceChannel:
    id = 2

    def __init__(self, connected: asyncio.Event, release: asyncio.Event) -> None:
        self.connected = connected
        self.release = release
        self.connect_calls = 0
        self.voice_client = _FakePynchyVoiceClient()
        self.guild = _FakeDiscordGuild(1, "Pynchy", [])
        self.name = "General"

    async def connect(self, **_kwargs: object) -> _FakePynchyVoiceClient:
        self.connect_calls += 1
        self.connected.set()
        await self.release.wait()
        return self.voice_client


@dataclass(slots=True)
class _VoiceState:
    channel: object | None


@dataclass
class _VoiceConnectionDecryptHarness:
    dave_session: object
    can_encrypt: bool
    listeners: list[object] = field(default_factory=list)

    def add_socket_listener(self, listener: object) -> None:
        self.listeners.append(listener)

    def remove_socket_listener(self, listener: object) -> None:
        self.listeners.remove(listener)


@dataclass(kw_only=True)
class _VoiceClientDecryptHarness(PynchyVoiceClient):
    _mode: str
    _secret_key: bytes
    _connection: _VoiceConnectionDecryptHarness
    _packet_listener: object | None = None
    _speaker_ids: dict[int, str] = field(default_factory=dict)
    _loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.get_running_loop)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def secret_key(self) -> bytes:
        return self._secret_key


async def _activate_voice_session(
    channel: DiscordChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> _FakePynchyVoiceClient:
    connected = asyncio.Event()
    release = asyncio.Event()
    release.set()
    voice_channel = _FakeVoiceChannel(connected, release)
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
        lambda _manager, _voice_channel: {"42": "Alice"},
    )
    monkeypatch.setattr("pynchy.plugins.channels.discord._voice._load_opus", lambda: True)

    await channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))

    return voice_channel.voice_client


class _FakeUser:
    def __init__(self, dm_channel: object | None = None) -> None:
        self.dm_channel = dm_channel
        self.create_dm_calls = 0
        self.created_dm = dm_channel or _FakeSendChannel()

    async def create_dm(self) -> object:
        self.create_dm_calls += 1
        self.dm_channel = self.created_dm
        return self.created_dm


@dataclass(slots=True)
class _FakeDiscordTextChannel:
    id: int
    name: str


@dataclass(slots=True)
class _FakeDiscordVoiceChannel:
    id: int
    name: str


class _FakeDiscordUser:
    def __init__(self, user_id: int, name: str, *, display_name: str | None = None) -> None:
        self.id = user_id
        self.name = name
        self.display_name = display_name or name
        self.global_name = display_name

    def __str__(self) -> str:
        return self.name


class _FakeDiscordGuild:
    def __init__(
        self,
        guild_id: int,
        name: str,
        channels: list[_FakeDiscordTextChannel],
        members: list[_FakeDiscordUser] | None = None,
        voice_channels: list[_FakeDiscordVoiceChannel] | None = None,
    ) -> None:
        self.id = guild_id
        self.name = name
        self.text_channels = channels
        self.members = members or []
        self.voice_channels = voice_channels or []
        self.created: list[str] = []

    async def create_text_channel(self, name: str, **kwargs) -> _FakeDiscordTextChannel:
        self.created.append(name)
        channel = _FakeDiscordTextChannel(789, name)
        self.text_channels.append(channel)
        return channel


class _FakeDiscordClient:
    def __init__(
        self, guilds: list[_FakeDiscordGuild], users: list[_FakeDiscordUser] | None = None
    ) -> None:
        self.guilds = guilds
        self.users = users or []

    def get_guild(self, guild_id: int) -> _FakeDiscordGuild | None:
        return next((guild for guild in self.guilds if guild.id == guild_id), None)

    async def fetch_guild(self, guild_id: int) -> _FakeDiscordGuild | None:
        return self.get_guild(guild_id)

    def get_all_members(self):
        for guild in self.guilds:
            yield from guild.members


@dataclass
class _DirectMessageClient:
    """The narrow discord.Client surface used by direct-message resolution."""

    get_user: object
    fetch_user: object


@dataclass
class _HistoryAuthor:
    id: str
    bot: bool
    display_name: str


@dataclass
class _HistoryChannel:
    id: str
    name: str | None = None
    parent: object | None = None
    parent_id: str | None = None


@dataclass
class _HistoryMessage:
    """SDK-shaped input that exercises Discord's parser at the history boundary."""

    id: str
    author: _HistoryAuthor
    channel: _HistoryChannel
    content: str
    created_at: datetime
    guild: object | None = None
    attachments: tuple[object, ...] = ()
    reference: object | None = None
    message_snapshots: tuple[object, ...] = ()
    mentions: tuple[object, ...] = ()
    type: object | None = None


def test_satisfies_channel_protocol():
    assert isinstance(_channel(), Channel)


def test_owns_only_discord_jids():
    ch = _channel()
    assert ch.owns_jid("discord:channel:1") is True
    assert ch.owns_jid("discord:direct:1") is True
    assert ch.owns_jid("slack:C1") is False


@pytest.mark.asyncio
async def test_creates_child_thread_for_scheduled_task():
    ch = _channel()
    parent = _FakeThreadParent()
    ch.resolve_channel = AsyncMock(return_value=parent)  # type: ignore[method-assign]

    child_jid = await ch.create_thread("discord:channel:123", "pynchy-dev-1")

    assert child_jid == "discord:channel:456"
    assert parent.thread_requests == [("pynchy-dev-1", discord.ChannelType.public_thread)]
    assert parent.sent_messages == ["Created thread: <#456>"]
    ch.resolve_channel.assert_awaited_once_with("discord:channel:123")


@pytest.mark.asyncio
async def test_adds_configured_default_to_child_thread():
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            default_thread_participants=["234"],
        )
    )
    parent = _FakeThreadParent()
    ch.resolve_channel = AsyncMock(return_value=parent)  # type: ignore[method-assign]

    await ch.create_thread(
        "discord:channel:123",
        "pynchy-dev-1",
    )

    assert parent.created_threads[0].added_user_ids == [234]


@pytest.mark.asyncio
async def test_adds_active_human_to_child_thread():
    ch = _channel()
    parent = _FakeThreadParent()
    ch.resolve_channel = AsyncMock(return_value=parent)  # type: ignore[method-assign]

    await ch.create_thread(
        "discord:channel:123",
        "pynchy-dev-1",
        participant_ids=("123", "not-a-discord-user", "123"),
    )

    assert parent.created_threads[0].added_user_ids == [123]


@pytest.mark.asyncio
async def test_finds_existing_active_child_thread_for_scheduled_task():
    ch = _channel()
    parent = _FakeThreadParent()
    parent.guild.threads = [
        _FakeThread(id=456, name="pynchy-dev-1", parent_id=parent.id),
        _FakeThread(id=123, name="pynchy-dev-1", parent_id=parent.id),
    ]
    ch.resolve_channel = AsyncMock(return_value=parent)  # type: ignore[method-assign]

    child_jid = await ch.find_thread("discord:channel:123", "pynchy-dev-1")

    assert child_jid == "discord:channel:123"
    assert parent.thread_requests == []


@pytest.mark.asyncio
async def test_reopens_archived_child_thread_instead_of_creating_a_duplicate():
    ch = _channel()
    parent = _FakeThreadParent()
    archived = _FakeThread(id=456, name="family", parent_id=parent.id, archived=True)
    parent.archived = [archived]
    ch.resolve_channel = AsyncMock(return_value=parent)  # type: ignore[method-assign]

    child_jid = await ch.find_thread("discord:channel:123", "family")

    assert child_jid == "discord:channel:456"
    assert archived.archived is False


@pytest.mark.asyncio
async def test_adds_active_human_to_reused_scheduled_child_thread():
    ch = _channel()
    thread = _FakeThread(id=456)
    ch.resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]

    await ch.add_thread_participants("discord:channel:456", ("123",))

    assert thread.added_user_ids == [123]


@pytest.mark.asyncio
async def test_voice_state_update_uses_homebrew_opus_fallback(monkeypatch):
    """A configured voice workspace starts receiving after the public gateway event."""
    loaded: list[str] = []
    monkeypatch.delenv("PYNCHY_DISCORD_OPUS_LIBRARY", raising=False)

    def load_opus(library: str) -> None:
        if library.endswith("libopus.0.dylib"):
            loaded.append(library)
            return
        raise OSError("not found")

    with (
        patch("pynchy.plugins.channels.discord._voice.find_library", return_value=None),
        patch("pynchy.plugins.channels.discord._voice.opus.is_loaded", return_value=False),
        patch("pynchy.plugins.channels.discord._voice.opus.load_opus", side_effect=load_opus),
    ):
        channel = _configured_voice_channel()
        connected = asyncio.Event()
        release = asyncio.Event()
        release.set()
        voice_channel = _FakeVoiceChannel(connected, release)
        monkeypatch.setattr(
            "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
            lambda _manager, _voice_channel: {"42": "Alice"},
        )

        await channel.handle_voice_state_update(
            object(),
            object(),
            _VoiceState(voice_channel),
        )

    assert loaded == ["/opt/homebrew/opt/opus/lib/libopus.0.dylib"]
    assert voice_channel.voice_client.received_listener is not None


@pytest.mark.asyncio
async def test_receive_voice_packet_preserves_rtp_extension_boundary():
    """RTP-size transport crypto authenticates only the extension preamble."""

    # Voice crypto is optional in ordinary Pynchy installs. Keeping this import
    # local lets the text-channel suite collect without the voice extra.
    davey = pytest.importorskip("davey")
    nacl_secret = pytest.importorskip("nacl.secret")

    class _FakeDaveSession:
        def __init__(self) -> None:
            self.packets: list[tuple[int, object, bytes]] = []

        def decrypt(self, user_id: int, media_type: object, packet: bytes) -> bytes:
            self.packets.append((user_id, media_type, packet))
            return packet

    secret_key = bytes(range(32))
    ssrc = 123
    header = struct.pack(">BBHII", 0x90, 0x78, 1, 2, ssrc) + b"\xbe\xde\x00\x01"
    extension_payload = b"\x10abc"
    dave_payload = b"encrypted-opus-frame"
    nonce = b"\x00\x00\x00\x01" + bytes(nacl_secret.Aead.NONCE_SIZE - 4)
    encrypted_payload = (
        nacl_secret.Aead(secret_key)
        .encrypt(extension_payload + dave_payload, header, nonce)
        .ciphertext
        + nonce[:4]
    )
    dave_session = _FakeDaveSession()
    voice_client = cast(
        "PynchyVoiceClient",
        _VoiceClientDecryptHarness(
            _mode="aead_xchacha20_poly1305_rtpsize",
            _secret_key=secret_key,
            _connection=_VoiceConnectionDecryptHarness(dave_session=dave_session, can_encrypt=True),
        ),
    )
    received: list[tuple[str, bytes]] = []

    def record_packet(speaker: str, packet: bytes) -> None:
        received.append((speaker, packet))

    PynchyVoiceClient.start_receiving(voice_client, record_packet)
    await PynchyVoiceClient.handle_voice_gateway_payload(
        voice_client,
        {"op": 5, "d": {"user_id": "42", "ssrc": ssrc}},
    )
    PynchyVoiceClient.receive_voice_packet(voice_client, header + encrypted_payload)
    await asyncio.sleep(0)

    assert received == [("42", dave_payload)]
    assert dave_session.packets[0][0] == 42
    assert dave_session.packets[0][1] is davey.MediaType.audio
    assert dave_session.packets[0][2] == dave_payload


@pytest.mark.asyncio
async def test_receive_voice_packet_retries_dave_transition_frame():
    nacl_secret = pytest.importorskip("nacl.secret")

    class _FakeDaveSession:
        def __init__(self) -> None:
            self.decrypt_attempts = 0
            self.passthrough: list[tuple[bool, int]] = []

        def decrypt(self, _user_id: int, _media_type: object, packet: bytes) -> bytes:
            self.decrypt_attempts += 1
            if self.decrypt_attempts == 1:
                raise ValueError(
                    "Failed to decrypt: DecryptionFailed(UnencryptedWhenPassthroughDisabled)"
                )
            return packet

        def set_passthrough_mode(self, enabled: bool, seconds: int) -> None:
            self.passthrough.append((enabled, seconds))

    secret_key = bytes(range(32))
    header = struct.pack(">BBHII", 0x80, 0x78, 1, 2, 3)
    dave_payload = b"transition-opus-frame"
    nonce = b"\x00\x00\x00\x01" + bytes(nacl_secret.Aead.NONCE_SIZE - 4)
    encrypted_payload = (
        nacl_secret.Aead(secret_key).encrypt(dave_payload, header, nonce).ciphertext + nonce[:4]
    )
    dave_session = _FakeDaveSession()
    voice_client = cast(
        "PynchyVoiceClient",
        _VoiceClientDecryptHarness(
            _mode="aead_xchacha20_poly1305_rtpsize",
            _secret_key=secret_key,
            _connection=_VoiceConnectionDecryptHarness(dave_session=dave_session, can_encrypt=True),
        ),
    )
    received: list[tuple[str, bytes]] = []

    def record_packet(speaker: str, packet: bytes) -> None:
        received.append((speaker, packet))

    PynchyVoiceClient.start_receiving(voice_client, record_packet)
    await PynchyVoiceClient.handle_voice_gateway_payload(
        voice_client,
        {"op": 5, "d": {"user_id": "42", "ssrc": 3}},
    )
    PynchyVoiceClient.receive_voice_packet(voice_client, header + encrypted_payload)
    await asyncio.sleep(0)

    assert received == [("42", dave_payload)]
    assert dave_session.passthrough == [(True, 10)]


@pytest.mark.asyncio
async def test_voice_manager_serializes_duplicate_connect_attempts(monkeypatch):
    channel = _configured_voice_channel()
    connected = asyncio.Event()
    release = asyncio.Event()
    voice_channel = _FakeVoiceChannel(connected, release)
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice.DiscordVoiceManager._allowed_members",
        lambda _manager, _voice_channel: {"42": "Alice"},
    )
    monkeypatch.setattr("pynchy.plugins.channels.discord._voice._load_opus", lambda: True)

    first = asyncio.create_task(
        channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))
    )
    await connected.wait()
    second = asyncio.create_task(
        channel.handle_voice_state_update(object(), object(), _VoiceState(voice_channel))
    )
    await asyncio.sleep(0)

    assert voice_channel.connect_calls == 1

    release.set()
    await asyncio.gather(first, second)

    assert voice_channel.connect_calls == 1


@pytest.mark.asyncio
async def test_voice_result_uses_wav_from_speech_synthesizer(monkeypatch):
    captured_suffixes: list[str] = []

    class FakeSpeechSynthesizer:
        name = "test"

        async def synthesize(self, _text: str, output_path: Path) -> SpeechSynthesisResult:
            captured_suffixes.append(output_path.suffix)
            return SpeechSynthesisResult(success=True, output_path=output_path, provider=self.name)

        async def health(self) -> SpeechSynthesizerHealth:
            return SpeechSynthesizerHealth(ready=True)

    synthesizer = FakeSpeechSynthesizer()
    channel = _configured_voice_channel(synthesizer)
    channel.client = object()
    voice_client = await _activate_voice_session(channel, monkeypatch)

    with patch("pynchy.plugins.channels.discord._voice.discord.FFmpegOpusAudio"):
        await channel.send_event(
            "discord:voice:2",
            OutboundEvent(type=OutboundEventType.RESULT, content="Hello"),
        )

    assert captured_suffixes == [".wav"]
    assert len(voice_client.played_audio) == 1


@pytest.mark.asyncio
async def test_voice_result_skips_playback_without_speech_provider(monkeypatch):
    channel = _configured_voice_channel()
    channel.client = object()
    voice_client = await _activate_voice_session(channel, monkeypatch)

    await channel.send_event(
        "discord:voice:2",
        OutboundEvent(type=OutboundEventType.RESULT, content="Hello"),
    )

    assert voice_client.played_audio == []


@pytest.mark.asyncio
async def test_resolve_chat_jid_maps_configured_guild_channel_ref():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            group_policy="allowlist",
            chat={"123": {"require_mention": False, "channels": {"456": {"enabled": True}}}},
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )

    assert await ch.resolve_chat_jid("123.channels.456") == "discord:channel:456"


@pytest.mark.asyncio
async def test_resolve_chat_jid_maps_allowed_direct_ref():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            allow_from=["discord:42"],
            group_policy="disabled",
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )

    assert await ch.resolve_chat_jid("direct.42") == "discord:direct:42"


@pytest.mark.asyncio
async def test_resolve_chat_jid_maps_allowed_direct_name_ref():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            allow_from=["alice"],
            group_policy="disabled",
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )
    user = _FakeDiscordUser(42, "asmith", display_name="Alice")
    ch.client = _FakeDiscordClient([], users=[user])

    assert await ch.resolve_chat_jid("direct.alice") == "discord:direct:42"


@pytest.mark.asyncio
async def test_resolve_chat_jid_maps_allowed_direct_name_ref_from_chat_metadata():
    await init_test_database()
    await store_chat_metadata("discord:direct:42", "2026-07-08T00:00:00+00:00", "Alice")
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            allow_from=["alice"],
            group_policy="disabled",
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )

    assert await ch.resolve_chat_jid("direct.alice") == "discord:direct:42"


@pytest.mark.asyncio
async def test_resolve_chat_jid_returns_none_for_unconfigured_channel_ref():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            group_policy="allowlist",
            chat={"123": {"require_mention": False, "channels": {}}},
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )

    assert await ch.resolve_chat_jid("123.channels.456") is None


@pytest.mark.asyncio
async def test_resolve_chat_jid_maps_configured_name_ref_to_existing_channel():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            group_policy="allowlist",
            chat={
                "synapse": {
                    "name": "Synapse",
                    "channels": {"code-improver": {"name": "code-improver"}},
                }
            },
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )
    ch.client = _FakeDiscordClient(
        [_FakeDiscordGuild(123, "Synapse", [_FakeDiscordTextChannel(456, "code-improver")])]
    )

    assert await ch.resolve_chat_jid("synapse.channels.code-improver") == "discord:channel:456"


@pytest.mark.asyncio
async def test_resolve_chat_jid_maps_configured_general_voice_channel_by_name():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            group_policy="allowlist",
            chat={
                "pynchy": {
                    "name": "Pynchy",
                    "channels": {"general": {"name": "General", "kind": "voice"}},
                }
            },
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )
    ch.client = _FakeDiscordClient(
        [
            _FakeDiscordGuild(
                123,
                "Pynchy",
                [],
                voice_channels=[_FakeDiscordVoiceChannel(456, "General")],
            )
        ]
    )

    assert await ch.resolve_chat_jid("pynchy.channels.general") == "discord:voice:456"


@pytest.mark.asyncio
async def test_create_group_creates_named_discord_channel():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            group_policy="allowlist",
            chat={
                "synapse": {
                    "name": "Synapse",
                    "channels": {"code-improver": {"name": "code-improver"}},
                }
            },
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )
    guild = _FakeDiscordGuild(123, "Synapse", [])
    ch.client = _FakeDiscordClient([guild])

    assert await ch.create_group("synapse.channels.code-improver") == "discord:channel:789"
    assert guild.created == ["code-improver"]


@pytest.mark.asyncio
async def test_create_group_creates_workspace_channel_from_display_name():
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="allowlist",
            group_policy="allowlist",
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
    )
    guild = _FakeDiscordGuild(123, "Synapse", [])
    ch.client = _FakeDiscordClient([guild])

    assert await ch.create_group("System Review") == "discord:channel:789"
    assert guild.created == ["system-review"]


@pytest.mark.asyncio
async def test_send_event_chunks_long_text_with_safe_mentions():
    ch = _channel()
    ch.client = object()  # non-None so the guard passes
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    long_text = "word " * 1000  # ~5000 chars -> multiple chunks
    await ch.send_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content=long_text)
    )

    assert len(fake.sends) > 1
    assert all(len(content) <= 2000 for content, _ in fake.sends)
    # every send suppresses accidental pings
    assert all(kw["allowed_mentions"] is not None for _, kw in fake.sends)


@pytest.mark.asyncio
async def test_send_event_skips_empty_text():
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve for empty text")
    )
    await ch.send_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="   ")
    )
    assert ch.resolve_channel.await_count == 0


@pytest.mark.asyncio
async def test_voice_channel_only_speaks_final_result():
    ch = _channel()
    ch.client = object()
    ch.voice.speak = AsyncMock()  # type: ignore[method-assign]

    await ch.send_event(
        "discord:voice:456",
        OutboundEvent(type=OutboundEventType.TEXT, content="draft"),
    )
    await ch.send_event(
        "discord:voice:456", OutboundEvent(type=OutboundEventType.RESULT, content="final reply")
    )

    ch.voice.speak.assert_awaited_once_with("discord:voice:456", "final reply")


@pytest.mark.asyncio
async def test_send_event_ignores_foreign_jid():
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve for a foreign jid")
    )
    await ch.send_event("slack:C1", OutboundEvent(type=OutboundEventType.TEXT, content="hi"))
    assert ch.resolve_channel.await_count == 0


@pytest.mark.asyncio
async def test_send_approval_event_posts_controls_and_routes_decision():
    decision_callback = MagicMock()
    ch = DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, dm_policy="open"),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, _msg: None,
        on_chat_metadata=lambda _jid, _ts, _name: None,
        on_approval_decision=decision_callback,
    )
    ch.client = object()
    fake = _FakeStreamChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    await ch.send_event(
        "discord:direct:42",
        OutboundEvent(
            type=OutboundEventType.APPROVAL,
            content="Approval required\n\n→ approve js / deny js",
            metadata={"short_id": "js"},
        ),
    )

    view = fake.sends[0][1]["view"]
    assert [item.label for item in view.children] == ["Approve", "Deny"]

    interaction = MagicMock()
    interaction.user.id = "42"
    interaction.user.bot = False
    interaction.user.roles = []
    interaction.channel.id = "42"
    interaction.channel.parent = None
    interaction.channel.parent_id = None
    interaction.channel.name = None
    interaction.guild = None
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()

    approve = view.children[0]
    await approve.callback(interaction)

    decision_callback.assert_called_once_with("discord:direct:42", "approve", "js", "42")
    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_reaction_ignores_non_discord_message_id():
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve for a non-Discord message id")
    )
    # slack-style id must be a no-op, not an error
    await ch.send_reaction("discord:channel:1", "slack-123", "u1", "👀")


@pytest.mark.asyncio
async def test_resolve_channel_caches_direct_message_channels():
    ch = _channel()

    user = _FakeUser()
    fetch_user = AsyncMock(return_value=user)
    ch.client = _DirectMessageClient(
        get_user=lambda _snowflake: None,
        fetch_user=fetch_user,
    )

    first = await ch.resolve_channel("discord:direct:42")
    second = await ch.resolve_channel("discord:direct:42")

    assert first is second
    assert fetch_user.await_count == 1
    assert user.create_dm_calls == 1


@pytest.mark.asyncio
async def test_disconnect_clears_direct_message_cache():
    ch = _channel()
    user = _FakeUser()
    fetch_user = AsyncMock(return_value=user)
    ch.client = _DirectMessageClient(
        get_user=lambda _snowflake: None,
        fetch_user=fetch_user,
    )
    ch.lifecycle.disconnect = AsyncMock()

    await ch.resolve_channel("discord:direct:42")
    await ch.disconnect()
    await ch.resolve_channel("discord:direct:42")

    assert fetch_user.await_count == 2


@pytest.mark.asyncio
async def test_set_typing_starts_background_refresh_and_stops_cleanly():
    ch = _channel()
    ch.client = object()
    fake = _FakeTypingChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.set_typing("discord:channel:1", is_typing=True)
    await asyncio.sleep(0)

    assert fake.typing_calls >= 1

    await ch.set_typing("discord:channel:1", is_typing=False)
    calls_after_stop = fake.typing_calls
    await asyncio.sleep(0)

    assert fake.typing_calls == calls_after_stop


@pytest.mark.asyncio
async def test_typing_loop_refreshes_until_cancelled(monkeypatch: pytest.MonkeyPatch):
    ch = _channel()
    ch.client = object()
    fake = _FakeTypingChannel()
    sleep_calls = 0
    orig_sleep = asyncio.sleep

    async def _fake_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        await orig_sleep(0)
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    await ch.set_typing("discord:channel:1", is_typing=True)
    for _ in range(3):
        await orig_sleep(0)

    assert fake.typing_calls == 2


@pytest.mark.asyncio
async def test_fetch_inbound_since_filters_bot_and_self():
    ch = _channel()
    ch.client = object()
    ch.bot_user_id = "self"

    def _msg(mid: str, author_id: str, *, bot: bool) -> _HistoryMessage:
        return _HistoryMessage(
            id=mid,
            author=_HistoryAuthor(id=author_id, bot=bot, display_name=f"user{author_id}"),
            channel=_HistoryChannel(id="1"),
            content=f"msg {mid}",
            created_at=datetime(2026, 7, 7, tzinfo=UTC),
        )

    class _HistChannel:
        def history(self, **kwargs):
            async def gen():
                await asyncio.sleep(0)
                yield _msg("1", "human", bot=False)
                yield _msg("2", "otherbot", bot=True)
                yield _msg("3", "self", bot=False)

            return gen()

    ch.resolve_channel = AsyncMock(return_value=_HistChannel())  # type: ignore[method-assign]

    result = await ch.fetch_inbound_since("discord:channel:1", "2026-07-06T00:00:00+00:00")
    ids = [m.id for m in result.messages]
    assert ids == ["discord-1"]  # bot + own filtered out
    assert result.high_water_mark


def test_plugin_returns_none_without_context():
    assert DiscordChannelPlugin().pynchy_create_channel(context=None) is None


@pytest.mark.asyncio
async def test_post_event_sends_preview_and_returns_message_id():
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    msg_id = await ch.post_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="hi there")
    )
    assert msg_id == "discord-101"
    assert fake.sends[0][0] == "hi there"
    # streamed previews must also use safe mention defaults
    assert fake.sends[0][1]["allowed_mentions"] is not None


@pytest.mark.asyncio
async def test_post_event_returns_none_for_empty_text():
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve for empty text")
    )
    result = await ch.post_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="   ")
    )
    assert result is None


@pytest.mark.asyncio
async def test_post_event_returns_none_when_too_large_to_stream():
    # A message over the single-message limit can't be an editable preview;
    # returning None makes core route it through chunked send_event instead.
    ch = _channel()
    ch.client = object()
    ch.resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve when text exceeds the limit")
    )
    result = await ch.post_event(
        "discord:channel:1",
        OutboundEvent(type=OutboundEventType.TEXT, content="x" * 2001),
    )
    assert result is None


@pytest.mark.asyncio
async def test_update_event_edits_message_in_place():
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()
    msg = await fake.send("initial", allowed_mentions=None)
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.update_event(
        "discord:channel:1",
        f"discord-{msg.id}",
        OutboundEvent(type=OutboundEventType.TEXT, content="updated text"),
    )
    assert msg.edits[-1][0] == "updated text"


@pytest.mark.asyncio
async def test_update_event_raises_when_too_large_so_core_falls_back():
    # Discord can't edit a message beyond the limit; raising lets sender.py
    # fall back to chunked send_event.
    ch = _channel()
    ch.client = object()
    fake = _FakeStreamChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="exceeds 2000 chars"):
        await ch.update_event(
            "discord:channel:1",
            "discord-101",
            OutboundEvent(type=OutboundEventType.TEXT, content="x" * 2001),
        )


def test_streaming_channel_satisfies_protocol_and_is_detected():
    ch = _channel()
    # core detects streaming targets via hasattr on both methods
    assert hasattr(ch, "post_event")
    assert hasattr(ch, "update_event")
