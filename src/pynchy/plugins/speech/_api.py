"""Speech synthesis provider contract and discovery helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
from typing import Protocol, TypeGuard, runtime_checkable

import pluggy  # noqa: TC002, RUF100 - beartype resolves plugin-manager annotations at runtime.

import pynchy.plugins as pynchy_plugins
from pynchy.logger import logger

__all__ = [
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


def get_speech_synthesizer(
    pm: pluggy.PluginManager | None = None,
) -> SpeechSynthesizer | None:
    """Discover the configured speech provider; the first valid provider wins."""
    providers = pynchy_plugins.collect_hook_results(
        "pynchy_speech_synthesizer",
        _is_valid_speech_synthesizer,
        "speech synthesizer",
        pm=pm,
    )
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
