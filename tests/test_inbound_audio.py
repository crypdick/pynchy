"""Shared inbound audio transcription behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from pynchy.host.inbound_audio import (
    InboundAudioAttachment,
    InboundAudioProcessingRequest,
    process_inbound_audio_attachments,
)

if TYPE_CHECKING:
    from pathlib import Path

VOICE_FALLBACK = "[Audio attachment received; transcription is not available yet: voice.ogg]"


async def test_process_inbound_audio_caches_transcribes_and_returns_metadata_patch(
    tmp_path: Path,
) -> None:
    def transcribe(path: Path) -> SimpleNamespace:
        assert path == tmp_path / "message_1-att_1.ogg"
        assert path.read_bytes() == b"voice bytes"
        return SimpleNamespace(
            success=True,
            transcript="ship the shared audio boundary",
            provider="local",
            model="base",
            error=None,
        )

    with patch(
        "pynchy.host.inbound_audio.transcribe_audio_file",
        new=AsyncMock(side_effect=transcribe),
    ):
        result = await process_inbound_audio_attachments(
            InboundAudioProcessingRequest(
                attachments=(
                    InboundAudioAttachment(
                        id="att/1",
                        filename="voice.ogg",
                        content_type="audio/ogg",
                        size=12,
                        data=b"voice bytes",
                    ),
                ),
                content="",
                fallback_content=VOICE_FALLBACK,
                cache_dir=tmp_path,
                message_id="message/1",
            )
        )

    assert result.content == (
        '[The user sent a voice message~ Here\'s what they said: "ship the shared audio boundary"]'
    )
    assert result.metadata_patches[0].index == 0
    assert result.metadata_patches[0].cached_path == str(tmp_path / "message_1-att_1.ogg")
    assert result.metadata_patches[0].transcription == {
        "success": True,
        "provider": "local",
        "model": "base",
    }


async def test_process_inbound_audio_without_audio_leaves_content_and_metadata_untouched(
    tmp_path: Path,
) -> None:
    result = await process_inbound_audio_attachments(
        InboundAudioProcessingRequest(
            attachments=(
                InboundAudioAttachment(
                    id="a1",
                    filename="design.txt",
                    content_type="text/plain",
                    size=12,
                    data=b"text bytes",
                ),
            ),
            content="please read later",
            fallback_content=VOICE_FALLBACK,
            cache_dir=tmp_path,
            message_id="m1",
        )
    )

    assert result.content == "please read later"
    assert result.metadata_patches == ()
    written_files = await asyncio.to_thread(lambda: list(tmp_path.iterdir()))
    assert written_files == []
