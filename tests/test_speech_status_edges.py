"""Status projection when a speech provider health check fails."""

from __future__ import annotations

import pytest

from pynchy.host.orchestrator.speech_status import collect_speech_status


class _BrokenSpeechProvider:
    name = "broken-speech"

    async def synthesize(self, _text, _output_path):
        raise AssertionError("not used")

    async def health(self):
        raise RuntimeError("provider offline")


@pytest.mark.asyncio
async def test_speech_status_reports_provider_health_failure() -> None:
    assert await collect_speech_status(_BrokenSpeechProvider()) == {
        "provider": "broken-speech",
        "ready": False,
        "endpoint": None,
        "error": "Speech synthesis health check failed: provider offline",
    }
