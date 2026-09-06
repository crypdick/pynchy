"""Speech-synthesis status collector for /status."""

from __future__ import annotations

from typing import Any

from pynchy.logger import logger
from pynchy.plugins.speech.api import (
    SpeechSynthesizer,
)


async def collect_speech_status(synthesizer: SpeechSynthesizer | None) -> dict[str, Any]:
    """Report the configured speech provider without breaking /status on failure."""
    if synthesizer is None:
        return {
            "provider": None,
            "ready": False,
            "endpoint": None,
            "error": "No speech synthesis provider is configured",
        }
    try:
        health = await synthesizer.health()
    except Exception as exc:  # noqa: BLE001 - status must report plugin failures without failing /status.
        logger.warning(
            "Speech synthesis health check failed",
            provider=synthesizer.name,
            err=str(exc),
        )
        return {
            "provider": synthesizer.name,
            "ready": False,
            "endpoint": None,
            "error": f"Speech synthesis health check failed: {exc}",
        }
    return {
        "provider": synthesizer.name,
        "ready": health.ready,
        "endpoint": health.endpoint,
        "error": health.error,
    }
