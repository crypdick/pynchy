"""Pocket TTS speech synthesis provider."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
import pluggy

from pynchy.logger import logger
from pynchy.plugins.speech.api import SpeechSynthesisResult, SpeechSynthesizerHealth

hookimpl = pluggy.HookimplMarker("pynchy")

DEFAULT_POCKET_TTS_ENDPOINT = "http://127.0.0.1:8000/tts"
_SYNTHESIS_TIMEOUT_SECONDS = 30
_HEALTH_TIMEOUT_SECONDS = 2


class PocketTtsProvider:
    """Synthesize WAV responses through a loopback Pocket TTS service."""

    name = "pocket-tts"

    def __init__(self, endpoint: str = DEFAULT_POCKET_TTS_ENDPOINT) -> None:
        self._endpoint = endpoint

    async def synthesize(self, text: str, output_path: Path) -> SpeechSynthesisResult:
        """Write Pocket TTS audio for ``text`` to ``output_path``."""
        content = text.strip()
        if not content:
            return SpeechSynthesisResult(
                success=False,
                provider=self.name,
                error="Cannot synthesize empty text",
            )
        try:
            audio = await self._request_audio(content)
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            logger.warning(
                "Pocket TTS synthesis failed",
                output_path=str(output_path),
                err=str(exc),
            )
            return SpeechSynthesisResult(
                success=False,
                provider=self.name,
                error=f"Pocket TTS synthesis failed: {exc}",
            )
        if not audio:
            return SpeechSynthesisResult(
                success=False,
                provider=self.name,
                error="Pocket TTS returned empty audio",
            )
        try:
            await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(output_path.write_bytes, audio)
        except OSError as exc:
            return SpeechSynthesisResult(
                success=False,
                provider=self.name,
                error=f"Failed to save Pocket TTS audio: {exc}",
            )
        return SpeechSynthesisResult(
            success=True,
            output_path=output_path,
            provider=self.name,
        )

    async def health(self) -> SpeechSynthesizerHealth:
        """Report whether the loopback Pocket TTS service accepts requests."""
        health_endpoint = self._health_endpoint()
        timeout = aiohttp.ClientTimeout(total=_HEALTH_TIMEOUT_SECONDS)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(health_endpoint) as response,
            ):
                if 200 <= response.status < 300:
                    return SpeechSynthesizerHealth(ready=True, endpoint=health_endpoint)
                return SpeechSynthesizerHealth(
                    ready=False,
                    endpoint=health_endpoint,
                    error=f"Pocket TTS returned HTTP {response.status}",
                )
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            return SpeechSynthesizerHealth(
                ready=False,
                endpoint=health_endpoint,
                error=f"Pocket TTS is unavailable: {exc}",
            )

    async def _request_audio(self, text: str) -> bytes:
        form = aiohttp.FormData()
        form.add_field("text", text)
        timeout = aiohttp.ClientTimeout(total=_SYNTHESIS_TIMEOUT_SECONDS)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(self._endpoint, data=form) as response,
        ):
            response.raise_for_status()
            return await response.read()

    def _health_endpoint(self) -> str:
        return f"{self._endpoint.rsplit('/', maxsplit=1)[0]}/"


class PocketTtsPlugin:  # noqa: V102
    """Built-in plugin that supplies the Pocket TTS provider."""

    @hookimpl
    def pynchy_speech_synthesizer(self) -> PocketTtsProvider:
        return PocketTtsProvider()
