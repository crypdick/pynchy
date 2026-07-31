"""Defensive coverage for the Discord voice transport boundary."""

from __future__ import annotations

import asyncio
import struct
from typing import Any, cast

import discord
import pytest
from discord.voice_state import VoiceConnectionState

from pynchy.plugins.channels.discord import PynchyVoiceClient
from tests.discord_channel_support import (
    _VoiceClientDecryptHarness,
    _VoiceConnectionDecryptHarness,
)


def _voice_client(connection=None) -> PynchyVoiceClient:
    connection = connection or _VoiceConnectionDecryptHarness(dave_session=None, can_encrypt=False)
    return cast(
        "PynchyVoiceClient",
        _VoiceClientDecryptHarness(
            _mode="not-encrypted",
            _secret_key=b"0" * 32,
            _connection=connection,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"op": 4},
        {"op": 5},
        {"op": 5, "d": None},
        {"op": 5, "d": {"user_id": None, "ssrc": 1}},
        {"op": 5, "d": {"user_id": "42", "ssrc": "1"}},
    ],
)
async def test_voice_gateway_ignores_non_speaking_or_malformed_payloads(payload):
    await PynchyVoiceClient.handle_voice_gateway_payload(_voice_client(), payload)


@pytest.mark.asyncio
async def test_voice_client_initializes_connection_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discord.VoiceClient, "__init__", lambda *_args: None)

    voice_client = PynchyVoiceClient(cast("Any", object()), cast("Any", object()))
    state = voice_client.create_connection_state()

    assert isinstance(state, VoiceConnectionState)
    assert state.hook is not None
    await state.hook(object(), {"op": 5, "d": {"user_id": 42, "ssrc": 3}})


@pytest.mark.asyncio
async def test_start_receiving_replaces_listener_and_stop_is_idempotent():
    await asyncio.sleep(0)
    connection = _VoiceConnectionDecryptHarness(dave_session=None, can_encrypt=False)
    voice_client = _voice_client(connection)
    listeners: list[tuple[str, bytes]] = []

    def record(user: str, packet: bytes) -> None:
        listeners.append((user, packet))

    PynchyVoiceClient.start_receiving(voice_client, record)
    PynchyVoiceClient.start_receiving(voice_client, record)
    assert len(connection.listeners) == 1

    PynchyVoiceClient.stop_receiving(voice_client)
    PynchyVoiceClient.stop_receiving(voice_client)
    assert connection.listeners == []


@pytest.mark.asyncio
async def test_receive_voice_packet_discards_malformed_unmapped_and_unencrypted_packets():
    await asyncio.sleep(0)
    connection = _VoiceConnectionDecryptHarness(dave_session=None, can_encrypt=False)
    voice_client = _voice_client(connection)
    received: list[tuple[str, bytes]] = []
    PynchyVoiceClient.receive_voice_packet(voice_client, b"")

    def record(user: str, packet: bytes) -> None:
        received.append((user, packet))

    PynchyVoiceClient.start_receiving(voice_client, record)

    malformed_extension = struct.pack(">BBHII", 0x90, 0x78, 1, 2, 3) + b"\x00\x00"
    fixed_header = struct.pack(">BBHII", 0x80, 0x78, 1, 2, 3)
    PynchyVoiceClient.receive_voice_packet(voice_client, malformed_extension)
    PynchyVoiceClient.receive_voice_packet(voice_client, fixed_header)
    PynchyVoiceClient.receive_voice_packet(voice_client, fixed_header + b"payload")

    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "dave_session", "can_encrypt"),
    [
        ("not-encrypted", object(), True),
        ("aead_xchacha20_poly1305_rtpsize", None, True),
        ("aead_xchacha20_poly1305_rtpsize", object(), False),
    ],
)
async def test_receive_voice_packet_discards_unavailable_decryption(
    mode: str, dave_session: object | None, can_encrypt: bool
) -> None:
    connection = _VoiceConnectionDecryptHarness(dave_session=dave_session, can_encrypt=can_encrypt)
    voice_client = cast(
        "PynchyVoiceClient",
        _VoiceClientDecryptHarness(
            _mode=mode,
            _secret_key=b"0" * 32,
            _connection=connection,
        ),
    )
    received: list[tuple[str, bytes]] = []
    PynchyVoiceClient.start_receiving(voice_client, lambda _user, _packet: None)
    await PynchyVoiceClient.handle_voice_gateway_payload(
        voice_client,
        {"op": 5, "d": {"user_id": "42", "ssrc": 3}},
    )

    PynchyVoiceClient.receive_voice_packet(
        voice_client,
        struct.pack(">BBHII", 0x80, 0x78, 1, 2, 3) + b"encrypted",
    )

    assert received == []


class _FakeMediaType:
    audio = object()


class _FakeDavey:
    MediaType = _FakeMediaType


class _FakeCryptoError(Exception):
    pass


class _FakeNaclExceptions:
    CryptoError = _FakeCryptoError


class _FakeAead:
    NONCE_SIZE = 24
    transport_payload = b"opus"
    error: Exception | None = None

    def __init__(self, _key: bytes) -> None:
        pass

    def decrypt(self, _ciphertext: bytes, _header: bytes, _nonce: bytes) -> bytes:
        if self.error is not None:
            raise self.error
        return self.transport_payload


class _FakeNaclSecret:
    Aead = _FakeAead


class _FakeDaveSession:
    def __init__(self, errors: list[ValueError] | None = None, result: object = b"opus") -> None:
        self.errors = errors or []
        self.result = result
        self.calls: list[tuple[int, object, bytes]] = []
        self.passthrough: list[tuple[bool, int]] = []

    def decrypt(self, user_id: int, media_type: object, payload: bytes) -> object:
        self.calls.append((user_id, media_type, payload))
        if self.errors:
            raise self.errors.pop(0)
        return self.result

    def set_passthrough_mode(self, enabled: bool, seconds: int) -> None:
        self.passthrough.append((enabled, seconds))


def _patch_voice_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    modules = {
        "davey": _FakeDavey,
        "nacl.exceptions": _FakeNaclExceptions,
        "nacl.secret": _FakeNaclSecret,
    }
    monkeypatch.setattr(
        "pynchy.plugins.channels.discord._voice_client.import_module",
        lambda name: modules[name],
    )


@pytest.mark.asyncio
async def test_receive_voice_packet_decrypts_transport_and_dave_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_voice_crypto(monkeypatch)
    dave_session = _FakeDaveSession()
    voice_client = cast(
        "PynchyVoiceClient",
        _VoiceClientDecryptHarness(
            _mode="aead_xchacha20_poly1305_rtpsize",
            _secret_key=b"0" * 32,
            _connection=_VoiceConnectionDecryptHarness(dave_session=dave_session, can_encrypt=True),
        ),
    )
    received: list[tuple[str, bytes]] = []
    PynchyVoiceClient.start_receiving(
        voice_client, lambda user, packet: received.append((user, packet))
    )
    await PynchyVoiceClient.handle_voice_gateway_payload(
        voice_client,
        {"op": 5, "d": {"user_id": "42", "ssrc": 3}},
    )

    PynchyVoiceClient.receive_voice_packet(
        voice_client,
        struct.pack(">BBHII", 0x80, 0x78, 1, 2, 3) + b"ciphertext" + b"\x00\x00\x00\x01",
    )
    await asyncio.sleep(0)

    assert received == [("42", b"opus")]
    assert dave_session.calls == [(42, _FakeMediaType.audio, b"opus")]


@pytest.mark.asyncio
async def test_receive_voice_packet_discards_transport_crypto_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_voice_crypto(monkeypatch)
    _FakeAead.error = _FakeCryptoError("bad transport")
    try:
        dave_session = _FakeDaveSession()
        voice_client = cast(
            "PynchyVoiceClient",
            _VoiceClientDecryptHarness(
                _mode="aead_xchacha20_poly1305_rtpsize",
                _secret_key=b"0" * 32,
                _connection=_VoiceConnectionDecryptHarness(
                    dave_session=dave_session, can_encrypt=True
                ),
            ),
        )
        received: list[tuple[str, bytes]] = []
        PynchyVoiceClient.start_receiving(
            voice_client, lambda user, packet: received.append((user, packet))
        )
        await PynchyVoiceClient.handle_voice_gateway_payload(
            voice_client,
            {"op": 5, "d": {"user_id": "42", "ssrc": 3}},
        )
        PynchyVoiceClient.receive_voice_packet(
            voice_client,
            struct.pack(">BBHII", 0x80, 0x78, 1, 2, 3) + b"ciphertext" + b"\x00\x00\x00\x01",
        )
        await asyncio.sleep(0)
        assert received == []
    finally:
        _FakeAead.error = None


@pytest.mark.asyncio
async def test_receive_voice_packet_rejects_transport_payload_shorter_than_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_voice_crypto(monkeypatch)
    _FakeAead.transport_payload = b"short"
    try:
        voice_client = cast(
            "PynchyVoiceClient",
            _VoiceClientDecryptHarness(
                _mode="aead_xchacha20_poly1305_rtpsize",
                _secret_key=b"0" * 32,
                _connection=_VoiceConnectionDecryptHarness(
                    dave_session=_FakeDaveSession(), can_encrypt=True
                ),
            ),
        )
        received: list[tuple[str, bytes]] = []
        PynchyVoiceClient.start_receiving(
            voice_client, lambda user, packet: received.append((user, packet))
        )
        await PynchyVoiceClient.handle_voice_gateway_payload(
            voice_client,
            {"op": 5, "d": {"user_id": "42", "ssrc": 3}},
        )
        header = struct.pack(">BBHII", 0x90, 0x78, 1, 2, 3) + b"\xbe\xde\x00\x02"
        PynchyVoiceClient.receive_voice_packet(
            voice_client, header + b"ciphertext" + b"\x00\x00\x00\x01"
        )
        await asyncio.sleep(0)
        assert received == []
    finally:
        _FakeAead.transport_payload = b"opus"


@pytest.mark.asyncio
async def test_receive_voice_packet_enables_dave_passthrough_for_transition_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_voice_crypto(monkeypatch)
    dave_session = _FakeDaveSession(
        errors=[ValueError("UnencryptedWhenPassthroughDisabled")],
    )
    voice_client = cast(
        "PynchyVoiceClient",
        _VoiceClientDecryptHarness(
            _mode="aead_xchacha20_poly1305_rtpsize",
            _secret_key=b"0" * 32,
            _connection=_VoiceConnectionDecryptHarness(dave_session=dave_session, can_encrypt=True),
        ),
    )
    received: list[tuple[str, bytes]] = []
    PynchyVoiceClient.start_receiving(
        voice_client, lambda user, packet: received.append((user, packet))
    )
    await PynchyVoiceClient.handle_voice_gateway_payload(
        voice_client,
        {"op": 5, "d": {"user_id": "42", "ssrc": 3}},
    )
    PynchyVoiceClient.receive_voice_packet(
        voice_client,
        struct.pack(">BBHII", 0x80, 0x78, 1, 2, 3) + b"ciphertext" + b"\x00\x00\x00\x01",
    )
    await asyncio.sleep(0)

    assert received == [("42", b"opus")]
    assert dave_session.passthrough == [(True, 10)]


@pytest.mark.asyncio
async def test_receive_voice_packet_drops_transition_frame_when_retry_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_voice_crypto(monkeypatch)
    dave_session = _FakeDaveSession(
        errors=[
            ValueError("UnencryptedWhenPassthroughDisabled"),
            ValueError("still unavailable"),
        ]
    )
    voice_client = cast(
        "PynchyVoiceClient",
        _VoiceClientDecryptHarness(
            _mode="aead_xchacha20_poly1305_rtpsize",
            _secret_key=b"0" * 32,
            _connection=_VoiceConnectionDecryptHarness(dave_session=dave_session, can_encrypt=True),
        ),
    )
    received: list[tuple[str, bytes]] = []
    PynchyVoiceClient.start_receiving(
        voice_client, lambda user, packet: received.append((user, packet))
    )
    await PynchyVoiceClient.handle_voice_gateway_payload(
        voice_client,
        {"op": 5, "d": {"user_id": "42", "ssrc": 3}},
    )
    PynchyVoiceClient.receive_voice_packet(
        voice_client,
        struct.pack(">BBHII", 0x80, 0x78, 1, 2, 3) + b"ciphertext" + b"\x00\x00\x00\x01",
    )
    await asyncio.sleep(0)

    assert received == []
    assert dave_session.passthrough == [(True, 10)]


@pytest.mark.asyncio
async def test_receive_voice_packet_discards_non_transition_dave_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_voice_crypto(monkeypatch)
    dave_session = _FakeDaveSession(errors=[ValueError("bad dave frame")])
    voice_client = cast(
        "PynchyVoiceClient",
        _VoiceClientDecryptHarness(
            _mode="aead_xchacha20_poly1305_rtpsize",
            _secret_key=b"0" * 32,
            _connection=_VoiceConnectionDecryptHarness(dave_session=dave_session, can_encrypt=True),
        ),
    )
    received: list[tuple[str, bytes]] = []
    PynchyVoiceClient.start_receiving(
        voice_client, lambda user, packet: received.append((user, packet))
    )
    await PynchyVoiceClient.handle_voice_gateway_payload(
        voice_client,
        {"op": 5, "d": {"user_id": "42", "ssrc": 3}},
    )
    PynchyVoiceClient.receive_voice_packet(
        voice_client,
        struct.pack(">BBHII", 0x80, 0x78, 1, 2, 3) + b"ciphertext" + b"\x00\x00\x00\x01",
    )
    await asyncio.sleep(0)

    assert received == []
