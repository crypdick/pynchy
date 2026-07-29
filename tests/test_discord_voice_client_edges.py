"""Defensive coverage for the Discord voice transport boundary."""

from __future__ import annotations

import asyncio
import struct
from typing import cast

import pytest

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
