"""Tests for DiscordChannel outbound behavior and protocol conformance.

The discord.py client is faked; these cover the channel's own logic (jid
ownership, chunked sending with safe mention defaults, reaction id handling,
history catch-up filtering) rather than the gateway glue in _lifecycle.
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

import discord
import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.api import (
    Channel,
    OutboundEvent,
    OutboundEventType,
)
from pynchy.plugins.channels.discord import DiscordChannel, PynchyVoiceClient
from pynchy.plugins.speech.api import SpeechSynthesisResult, SpeechSynthesizerHealth
from pynchy.state import init_test_database, store_chat_metadata
from pynchy.state.api import get_chat_jids_by_name
from tests.discord_channel_support import (
    _activate_voice_session,
    _channel,
    _configured_voice_channel,
    _FakeDiscordClient,
    _FakeDiscordGuild,
    _FakeDiscordTextChannel,
    _FakeDiscordUser,
    _FakeDiscordVoiceChannel,
    _FakeSendChannel,
    _FakeThread,
    _FakeThreadParent,
    _FakeVoiceChannel,
    _VoiceClientDecryptHarness,
    _VoiceConnectionDecryptHarness,
    _VoiceState,
)

DISCORD_BOT_ENV = "X"
DISCORD_BOT_VALUE = "token"


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
async def test_reports_whether_resolved_target_can_host_child_threads():
    ch = _channel()
    parent = _FakeThreadParent()
    child = _FakeThread(id=456)
    ch.resolve_channel = AsyncMock(side_effect=[parent, child])  # type: ignore[method-assign]

    assert await ch.supports_child_threads("discord:channel:123") is True
    assert await ch.supports_child_threads("discord:channel:456") is False


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
async def test_maps_conversation_closed_state_to_thread_archival():
    ch = _channel()
    thread = _FakeThread(id=456)
    ch.resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]

    await ch.set_thread_closed("discord:channel:456", closed=True)
    await ch.set_thread_closed("discord:channel:456", closed=True)
    await ch.set_thread_closed("discord:channel:456", closed=False)

    assert thread.archived is False
    assert thread.archive_edits == [True, False]


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
        audio_cache_dir=Path("data/media/discord"),
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
        audio_cache_dir=Path("data/media/discord"),
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
        audio_cache_dir=Path("data/media/discord"),
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
        audio_cache_dir=Path("data/media/discord"),
        find_chat_jids_by_name=get_chat_jids_by_name,
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
        audio_cache_dir=Path("data/media/discord"),
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
        audio_cache_dir=Path("data/media/discord"),
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
        audio_cache_dir=Path("data/media/discord"),
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
        audio_cache_dir=Path("data/media/discord"),
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
        audio_cache_dir=Path("data/media/discord"),
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
async def test_send_result_uses_discord_identity_without_mutating_shared_event():
    ch = _channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    event = OutboundEvent(
        type=OutboundEventType.RESULT,
        content="final reply",
        metadata={"turn_id": "turn-1"},
    )

    await ch.send_event("discord:channel:1", event)

    assert fake.sends[0][0] == "final reply"
    assert event.metadata == {"turn_id": "turn-1"}


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
