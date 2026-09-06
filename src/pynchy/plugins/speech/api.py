"""Speech synthesis provider contract and discovery helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeGuard, runtime_checkable

import pluggy

from pynchy.logger import logger

__all__ = [
    "SpeechSynthesisProvider",
    "SpeechSynthesisResult",
    "SpeechSynthesizer",
    "SpeechSynthesizerHealth",
    "get_speech_synthesizer",
]


@dataclass(frozen=True)
class SpeechSynthesisResult:
    """Outcome of one request to synthesize speech into a local file."""

    success: bool
    output_path: Path | None = None
    provider: str = "none"
    error: str | None = None


@dataclass(frozen=True)
class SpeechSynthesizerHealth:
    """Readiness report returned by a speech synthesis provider."""

    ready: bool
    endpoint: str | None = None
    error: str | None = None


@runtime_checkable
class SpeechSynthesisProvider(Protocol):
    """Provider subset required to render one spoken reply."""

    async def synthesize(self, text: str, output_path: Path) -> SpeechSynthesisResult: ...


@runtime_checkable
class SpeechSynthesizer(Protocol):
    """Host-side provider for final spoken channel replies."""

    name: str

    async def synthesize(self, text: str, output_path: Path) -> SpeechSynthesisResult: ...

    async def health(self) -> SpeechSynthesizerHealth: ...


def _is_valid_speech_synthesizer(candidate: object) -> TypeGuard[SpeechSynthesizer]:
    return all(
        [
            hasattr(candidate, "name"),
            callable(getattr(candidate, "synthesize", None)),
            callable(getattr(candidate, "health", None)),
        ]
    )


def get_speech_synthesizer(pm: pluggy.PluginManager) -> SpeechSynthesizer | None:
    """Discover the configured speech provider; the first valid provider wins."""
    try:
        candidates = pm.hook.pynchy_speech_synthesizer()
    except Exception:  # noqa: BLE001 - one plugin must not break speech discovery.
        logger.exception("Failed to resolve speech synthesizer plugins")
        return None
    providers = [candidate for candidate in candidates if _is_valid_speech_synthesizer(candidate)]
    if not providers:
        logger.info("No speech synthesis provider registered")
        return None
    if len(providers) > 1:
        logger.warning(
            "Multiple speech synthesis providers registered; using the first",
            providers=[provider.name for provider in providers],
        )
    logger.info("Speech synthesis provider discovered", name=providers[0].name)
    return providers[0]
