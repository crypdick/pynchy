"""Tests for configured host-side text-to-speech synthesis."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pynchy.host.audio import LOCAL_TTS_COMMAND_ENV, synthesize_speech_to_file


@pytest.mark.asyncio
async def test_synthesis_reports_missing_provider(tmp_path, monkeypatch):
    monkeypatch.delenv(LOCAL_TTS_COMMAND_ENV, raising=False)

    result = await synthesize_speech_to_file("Hello", tmp_path / "reply.wav")

    assert result.success is False
    assert LOCAL_TTS_COMMAND_ENV in str(result.error)


@pytest.mark.asyncio
async def test_synthesis_runs_configured_command_and_returns_audio(tmp_path, monkeypatch):
    output_path = tmp_path / "reply.wav"
    monkeypatch.setenv(LOCAL_TTS_COMMAND_ENV, "tts {input_path} {output_path}")

    def synthesize(command: list[str]) -> None:
        assert Path(command[1]).read_text(encoding="utf-8") == "Hello"
        Path(command[2]).write_bytes(b"audio")

    with patch("pynchy.host.audio._run_local_command", side_effect=synthesize):
        result = await synthesize_speech_to_file("Hello", output_path)

    assert result.success is True
    assert result.output_path == output_path
