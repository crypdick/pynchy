"""Tests for DiscordChannel outbound behavior and protocol conformance.

The discord.py client is faked; these cover the channel's own logic (jid
ownership, chunked sending with safe mention defaults, reaction id handling,
history catch-up filtering) rather than the gateway glue in _lifecycle.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.config.models import DiscordConnectionConfig
from pynchy.host.audio import AudioSynthesisResult
from pynchy.plugins.channels.discord import DiscordChannel, DiscordChannelPlugin
from pynchy.plugins.channels.discord._voice import (  # noqa: PLC2701
    DiscordVoiceManager,
    _load_opus,  # allow: private-test-imports - platform Opus loader.
    _VoiceSession,  # allow: private-test-imports - voice playback boundary.
)
from pynchy.plugins.channels.discord._voice_client import (  # noqa: PLC2701
    PynchyVoiceClient,
    _parse_rtp_packet,  # allow: private-test-imports - exercises RTP crypto boundary.
)
from pynchy.state import init_test_database, store_chat_metadata
from pynchy.types import Channel, OutboundEvent, OutboundEventType, WorkspaceProfile

DISCORD_BOT_ENV = "X"
DISCORD_BOT_VALUE = "token"

if TYPE_CHECKING:
    from pathlib import Path


def _channel() -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
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


class _FakePynchyVoiceClient(PynchyVoiceClient):
    def __init__(self) -> None:
        pass

    def is_connected(self) -> bool:
        return True


class _FakeReceivingVoiceClient(PynchyVoiceClient):
    def __init__(self) -> None:
        pass

    def start_receiving(self, _listener: object) -> None:
        pass


class _FakeVoiceChannel:
    id = 2

    def __init__(self, connected: asyncio.Event, release: asyncio.Event) -> None:
        self.connected = connected
        self.release = release
        self.connect_calls = 0

    async def connect(self, **_kwargs: object) -> _FakeReceivingVoiceClient:
        self.connect_calls += 1
        self.connected.set()
        await self.release.wait()
        return _FakeReceivingVoiceClient()


@dataclass
class _VoiceConnectionDecryptHarness:
    dave_session: object
    can_encrypt: bool


@dataclass
class _VoiceClientDecryptHarness:
    mode: str
    secret_key: bytes
    _connection: _VoiceConnectionDecryptHarness


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


def test_load_opus_uses_homebrew_fallback(monkeypatch):
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
        assert _load_opus() is True

    assert loaded == ["/opt/homebrew/opt/opus/lib/libopus.0.dylib"]


def test_decrypt_voice_payload_preserves_rtp_extension_boundary():
    """RTP-size transport crypto authenticates only the extension preamble."""

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
    parsed = _parse_rtp_packet(header + encrypted_payload)

    assert parsed is not None
    parsed_header, parsed_ssrc, payload, extension_length = parsed
    dave_session = _FakeDaveSession()
    voice_client = cast(
        "PynchyVoiceClient",
        _VoiceClientDecryptHarness(
            mode="aead_xchacha20_poly1305_rtpsize",
            secret_key=secret_key,
            _connection=_VoiceConnectionDecryptHarness(dave_session=dave_session, can_encrypt=True),
        ),
    )

    assert parsed_ssrc == ssrc
    assert extension_length == len(extension_payload)
    assert (
        PynchyVoiceClient._decrypt_voice_payload(
            voice_client,
            parsed_header,
            payload,
            "42",
            extension_length,
        )
        == dave_payload
    )
    assert dave_session.packets[0][0] == 42
    assert dave_session.packets[0][2] == dave_payload


def test_decrypt_voice_payload_retries_dave_transition_frame():
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
            mode="aead_xchacha20_poly1305_rtpsize",
            secret_key=secret_key,
            _connection=_VoiceConnectionDecryptHarness(dave_session=dave_session, can_encrypt=True),
        ),
    )

    assert (
        PynchyVoiceClient._decrypt_voice_payload(
            voice_client,
            header,
            encrypted_payload,
            "42",
            0,
        )
        == dave_payload
    )
    assert dave_session.passthrough == [(True, 10)]


@pytest.mark.asyncio
async def test_voice_manager_serializes_duplicate_connect_attempts(monkeypatch):
    channel = _channel()
    channel.workspaces = lambda: {"discord:voice:2": cast("WorkspaceProfile", object())}
    manager = DiscordVoiceManager(channel)
    connected = asyncio.Event()
    release = asyncio.Event()
    voice_channel = _FakeVoiceChannel(connected, release)
    monkeypatch.setattr(manager, "_allowed_members", lambda _voice_channel: {"42": "Alice"})
    monkeypatch.setattr("pynchy.plugins.channels.discord._voice._load_opus", lambda: True)

    first = asyncio.create_task(manager._refresh(voice_channel))
    await connected.wait()
    second = asyncio.create_task(manager._refresh(voice_channel))
    await asyncio.sleep(0)

    assert voice_channel.connect_calls == 1

    release.set()
    await asyncio.gather(first, second)

    assert voice_channel.connect_calls == 1


@pytest.mark.asyncio
async def test_voice_session_uses_aiff_for_macos_say():
    session = _VoiceSession(_channel(), "discord:voice:1", _FakePynchyVoiceClient(), {})
    captured_suffixes: list[str] = []

    def synthesize(_text: str, output_path: Path) -> AudioSynthesisResult:
        captured_suffixes.append(output_path.suffix)
        return AudioSynthesisResult(success=True, output_path=output_path)

    with (
        patch(
            "pynchy.plugins.channels.discord._voice.synthesize_speech_to_file",
            new=AsyncMock(side_effect=synthesize),
        ),
        patch.object(session, "_play_audio", new_callable=AsyncMock),
    ):
        await session.speak("Hello")

    assert captured_suffixes == [".aiff"]


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
    ch._resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

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
    ch._resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve for empty text")
    )
    await ch.send_event(
        "discord:channel:1", OutboundEvent(type=OutboundEventType.TEXT, content="   ")
    )
    assert ch._resolve_channel.await_count == 0


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
    ch._resolve_channel = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("should not resolve for a foreign jid")
    )
    await ch.send_event("slack:C1", OutboundEvent(type=OutboundEventType.TEXT, content="hi"))
    assert ch._resolve_channel.await_count == 0


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
    ch._resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

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
    ch._resolve_channel = AsyncMock(  # type: ignore[method-assign]
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

    first = await ch._resolve_channel("discord:direct:42")
    second = await ch._resolve_channel("discord:direct:42")

    assert first is second
    assert fetch_user.await_count == 1
    assert user.create_dm_calls == 1


@pytest.mark.asyncio
async def test_disconnect_clears_direct_message_cache():
    ch = _channel()
    ch._dm_channels["42"] = _FakeSendChannel()
    ch.lifecycle.disconnect = AsyncMock()

    await ch.disconnect()

    assert ch._dm_channels == {}


@pytest.mark.asyncio
async def test_set_typing_starts_background_refresh_and_stops_cleanly():
    ch = _channel()
    ch.client = object()
    fake = _FakeTypingChannel()
    ch._resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.set_typing("discord:channel:1", is_typing=True)
    await asyncio.sleep(0)

    assert fake.typing_calls >= 1
    assert "discord:channel:1" in ch._typing_tasks

    await ch.set_typing("discord:channel:1", is_typing=False)

    assert "discord:channel:1" not in ch._typing_tasks


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

    ch._resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await ch._typing_loop("discord:channel:1")

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

    ch._resolve_channel = AsyncMock(return_value=_HistChannel())  # type: ignore[method-assign]

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
    ch._resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
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
    ch._resolve_channel = AsyncMock(  # type: ignore[method-assign]
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
    ch._resolve_channel = AsyncMock(  # type: ignore[method-assign]
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
    ch._resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
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
    ch._resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
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
