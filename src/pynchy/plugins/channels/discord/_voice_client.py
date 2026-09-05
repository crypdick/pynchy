"""discord.py voice client extended with a narrow inbound Opus callback."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING, Any

import discord
from discord.voice_state import VoiceConnectionState

from pynchy.logger import logger

if TYPE_CHECKING:
    from discord.abc import Connectable
    from discord.client import Client
else:
    Client = object
    Connectable = object

_SPEAKING_OPCODE = 5
_RTP_VERSION = 2
_AEAD_MODE = "aead_xchacha20_poly1305_rtpsize"
_DAVE_PASSTHROUGH_SECONDS = 10
_DAVE_UNENCRYPTED_FRAME_ERROR = "UnencryptedWhenPassthroughDisabled"

VoicePacketListener = Callable[[str, bytes], None]


class PynchyVoiceClient(discord.VoiceClient):
    """Expose decrypted incoming Discord Opus packets to one host session."""

    def __init__(self, client: Client, channel: Connectable) -> None:
        self._packet_listener: VoicePacketListener | None = None
        self._speaker_ids: dict[int, str] = {}
        self._loop = asyncio.get_running_loop()
        super().__init__(client, channel)

    def create_connection_state(self) -> VoiceConnectionState:  # noqa: V105
        return VoiceConnectionState(self, hook=self._on_voice_gateway_payload)

    async def _on_voice_gateway_payload(self, _websocket: object, payload: dict[str, Any]) -> None:
        await self.handle_voice_gateway_payload(payload)

    async def handle_voice_gateway_payload(self, payload: dict[str, Any]) -> None:
        """Record a Discord voice-gateway SPEAKING notification."""
        if payload.get("op") != _SPEAKING_OPCODE:
            return
        data = payload.get("d")
        if not isinstance(data, dict):
            return
        user_id = data.get("user_id")
        ssrc = data.get("ssrc")
        if user_id is None or not isinstance(ssrc, int):
            return
        self._speaker_ids[ssrc] = str(user_id)

    def start_receiving(self, listener: VoicePacketListener) -> None:
        """Register the one session that consumes this connection's audio."""
        if self._packet_listener is not None:
            self.stop_receiving()
        self._packet_listener = listener
        self._connection.add_socket_listener(self.receive_voice_packet)

    def stop_receiving(self) -> None:
        if self._packet_listener is None:
            return
        self._connection.remove_socket_listener(self.receive_voice_packet)
        self._packet_listener = None
        self._speaker_ids.clear()

    def receive_voice_packet(self, packet: bytes) -> None:
        """Receive one encrypted Discord UDP voice packet from the socket reader."""
        listener = self._packet_listener
        parsed = _parse_rtp_packet(packet)
        if listener is None or parsed is None:
            return
        header, ssrc, encrypted_payload, extension_payload_length = parsed
        speaker_id = self._speaker_ids.get(ssrc)
        if speaker_id is None:
            return
        opus_packet = self._decrypt_voice_payload(
            header,
            encrypted_payload,
            speaker_id,
            extension_payload_length,
        )
        if opus_packet is None:
            return
        self._loop.call_soon_threadsafe(listener, speaker_id, opus_packet)

    def _decrypt_voice_payload(
        self,
        header: bytes,
        encrypted_payload: bytes,
        speaker_id: str,
        extension_payload_length: int,
    ) -> bytes | None:
        if self.mode != _AEAD_MODE or len(encrypted_payload) <= 4:
            return None
        session = self._connection.dave_session
        if session is None or not self._connection.can_encrypt:
            return None
        # Discord text support is intentionally usable without the ``voice`` extra.
        # discord.py guards its own voice-only imports, so do the same here rather
        # than breaking plugin discovery and test collection for text-only installs.
        davey = import_module("davey")
        nacl_exceptions = import_module("nacl.exceptions")
        nacl_secret = import_module("nacl.secret")
        nonce = bytearray(nacl_secret.Aead.NONCE_SIZE)
        nonce[:4] = encrypted_payload[-4:]
        try:
            transport_payload = nacl_secret.Aead(bytes(self.secret_key)).decrypt(
                encrypted_payload[:-4],
                header,
                bytes(nonce),
            )
        except nacl_exceptions.CryptoError:
            logger.debug("Discarded Discord voice packet that failed transport decryption")
            return None

        if len(transport_payload) < extension_payload_length:
            return None
        dave_payload = transport_payload[extension_payload_length:]
        return _decrypt_dave_payload(session, davey, speaker_id, dave_payload)


def _decrypt_dave_payload(
    session: object,
    davey: object,
    speaker_id: str,
    payload: bytes,
) -> bytes | None:
    try:
        decrypted = session.decrypt(int(speaker_id), davey.MediaType.audio, payload)
    except ValueError as exc:
        if _DAVE_UNENCRYPTED_FRAME_ERROR not in str(exc):
            logger.debug("Discarded Discord voice packet that failed DAVE decryption", err=str(exc))
            return None
        passthrough_enabled = True
        session.set_passthrough_mode(passthrough_enabled, _DAVE_PASSTHROUGH_SECONDS)
        try:
            decrypted = session.decrypt(int(speaker_id), davey.MediaType.audio, payload)
        except ValueError as retry_error:
            logger.debug(
                "Discarded Discord voice packet after enabling DAVE passthrough",
                err=str(retry_error),
            )
            return None
    return decrypted if isinstance(decrypted, bytes) else None


def _parse_rtp_packet(packet: bytes) -> tuple[bytes, int, bytes, int] | None:
    """Return the authenticated RTP header and encrypted packet fields.

    RTP-size encryption authenticates the fixed header, CSRCs, and extension
    preamble. The extension body remains transport-encrypted ahead of the Opus
    frame, so the DAVE payload begins after it.
    """
    if len(packet) < 12 or packet[0] >> 6 != _RTP_VERSION:
        return None
    header_length = 12 + (packet[0] & 0x0F) * 4
    extension_payload_length = 0
    if packet[0] & 0x10:
        if len(packet) < header_length + 4:
            return None
        extension_payload_length = struct.unpack_from(">H", packet, header_length + 2)[0] * 4
        header_length += 4
    if len(packet) <= header_length:
        return None
    return (
        packet[:header_length],
        struct.unpack_from(">I", packet, 8)[0],
        packet[header_length:],
        extension_payload_length,
    )
