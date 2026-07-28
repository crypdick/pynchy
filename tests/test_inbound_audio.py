"""Shared inbound audio transcription behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from pynchy.host.audio import (
    MAX_AUDIO_BYTES,
    AudioTranscriptionResult,
    InboundAudioAttachment,
    InboundAudioProcessingRequest,
    process_inbound_audio_attachments,
    transcribe_audio_file,
)

if TYPE_CHECKING:
    import pytest

VOICE_FALLBACK = "[Audio attachment received; transcription is not available yet: voice.ogg]"


async def test_process_inbound_audio_caches_transcribes_and_returns_metadata_patch(
    tmp_path: Path,
) -> None:
    def transcribe(path: Path) -> AudioTranscriptionResult:
        assert path == tmp_path / "message_1-att_1.ogg"
        assert path.read_bytes() == b"voice bytes"
        return AudioTranscriptionResult(
            success=True,
            transcript="ship the shared audio boundary",
            provider="local",
            model="base",
            error=None,
        )

    with patch(
        "pynchy.host.audio.transcribe_audio_file",
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


async def test_process_inbound_audio_reports_an_oversized_voice_attachment_without_writing_it(
    tmp_path: Path,
) -> None:
    result = await process_inbound_audio_attachments(
        InboundAudioProcessingRequest(
            attachments=(
                InboundAudioAttachment(
                    id="too-large",
                    filename="voice.ogg",
                    content_type="audio/ogg",
                    size=MAX_AUDIO_BYTES + 1,
                    data=b"not written",
                ),
            ),
            content="",
            fallback_content=VOICE_FALLBACK,
            cache_dir=tmp_path,
            message_id="m1",
        )
    )

    assert result.content == (
        f"[The user sent a voice message but I had trouble transcribing it~ "
        f"(Audio file is too large: {MAX_AUDIO_BYTES + 1} bytes (max {MAX_AUDIO_BYTES}))]"
    )
    assert result.metadata_patches[0].cached_path is None
    assert result.metadata_patches[0].transcription == {
        "success": False,
        "provider": "none",
        "error": f"Audio file is too large: {MAX_AUDIO_BYTES + 1} bytes (max {MAX_AUDIO_BYTES})",
    }
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


async def test_transcribe_audio_file_rejects_missing_and_unsupported_inputs(tmp_path: Path) -> None:
    missing = await transcribe_audio_file(tmp_path / "missing.ogg")
    unsupported_path = tmp_path / "voice.txt"
    unsupported_path.write_bytes(b"not audio")
    unsupported = await transcribe_audio_file(unsupported_path)

    assert missing.success is False
    assert missing.error == f"Audio file not found: {tmp_path / 'missing.ogg'}"
    assert unsupported.success is False
    assert unsupported.error == "Unsupported audio format: .txt"


async def test_transcribe_audio_file_runs_the_configured_local_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio_path = tmp_path / "voice.ogg"
    await asyncio.to_thread(audio_path.write_bytes, b"audio")
    monkeypatch.setenv(
        "PYNCHY_LOCAL_STT_COMMAND",
        "transcriber --output-dir {output_dir} --input {input_path} --model {model}",
    )

    def run_local_command(command: list[str], **kwargs: object) -> None:
        assert command[:2] == ["transcriber", "--output-dir"]
        assert "--input" in command
        assert command[-2:] == ["--model", "base"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 180, "check": True}
        Path(command[2]).joinpath("voice.txt").write_text("transcribed locally\n", encoding="utf-8")

    with patch("pynchy.host.audio.subprocess.run", side_effect=run_local_command):
        result = await transcribe_audio_file(audio_path)

    assert result == AudioTranscriptionResult(
        success=True,
        transcript="transcribed locally",
        provider="local_command",
        model="base",
    )
