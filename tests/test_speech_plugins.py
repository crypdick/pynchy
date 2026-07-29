"""Tests for the host-side speech synthesis plugin boundary."""

from __future__ import annotations

import pluggy
import pytest
from aiohttp import web

from pynchy.plugins.api import PynchySpec
from pynchy.plugins.speech.api import (
    SpeechSynthesisResult,
    SpeechSynthesizerHealth,
    get_speech_synthesizer,
)
from pynchy.plugins.speech.pocket_tts import PocketTtsPlugin, PocketTtsProvider

hookimpl = pluggy.HookimplMarker("pynchy")


class _InvalidSpeechPlugin:
    @hookimpl
    def pynchy_speech_synthesizer(self) -> object:
        return object()


class _BrokenSpeechPlugin:
    @hookimpl
    def pynchy_speech_synthesizer(self) -> None:
        raise RuntimeError("plugin startup failed")


class _SpeechProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def synthesize(self, _text: str, output_path):
        return SpeechSynthesisResult(success=True, output_path=output_path, provider=self.name)

    async def health(self) -> SpeechSynthesizerHealth:
        return SpeechSynthesizerHealth(ready=True)


class _SpeechPlugin:
    def __init__(self, provider: _SpeechProvider) -> None:
        self._provider = provider

    @hookimpl
    def pynchy_speech_synthesizer(self) -> _SpeechProvider:
        return self._provider


async def _start_server(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert server is not None
    port = server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_pocket_tts_posts_text_and_writes_wav(tmp_path):
    received: dict[str, str] = {}

    async def synthesize(request: web.Request) -> web.Response:
        form = await request.post()
        received["text"] = str(form["text"])
        return web.Response(body=b"audio", content_type="audio/wav")

    app = web.Application()
    app.router.add_post("/tts", synthesize)
    runner, endpoint = await _start_server(app)
    try:
        output_path = tmp_path / "reply.wav"
        result = await PocketTtsProvider(f"{endpoint}/tts").synthesize("Hello", output_path)
    finally:
        await runner.cleanup()

    assert received == {"text": "Hello"}
    assert result.success is True
    assert result.output_path == output_path
    assert result.provider == "pocket-tts"
    assert output_path.read_bytes() == b"audio"


@pytest.mark.asyncio
async def test_pocket_tts_reports_non_success_response(tmp_path):
    async def unavailable(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(status=503)

    app = web.Application()
    app.router.add_post("/tts", unavailable)
    runner, endpoint = await _start_server(app)
    try:
        result = await PocketTtsProvider(f"{endpoint}/tts").synthesize(
            "Hello", tmp_path / "reply.wav"
        )
    finally:
        await runner.cleanup()

    assert result.success is False
    assert result.provider == "pocket-tts"
    assert result.error is not None


@pytest.mark.asyncio
async def test_pocket_tts_rejects_an_empty_audio_response(tmp_path):
    async def empty_audio(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(body=b"", content_type="audio/wav")

    app = web.Application()
    app.router.add_post("/tts", empty_audio)
    runner, endpoint = await _start_server(app)
    try:
        result = await PocketTtsProvider(f"{endpoint}/tts").synthesize(
            "Hello", tmp_path / "reply.wav"
        )
    finally:
        await runner.cleanup()

    assert result.success is False
    assert result.error == "Pocket TTS returned empty audio"


@pytest.mark.asyncio
async def test_pocket_tts_rejects_empty_text(tmp_path):
    result = await PocketTtsProvider().synthesize("  ", tmp_path / "reply.wav")

    assert result.success is False
    assert result.error == "Cannot synthesize empty text"


@pytest.mark.asyncio
async def test_pocket_tts_reports_an_unwritable_audio_destination(tmp_path):
    async def synthesize(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(body=b"audio", content_type="audio/wav")

    app = web.Application()
    app.router.add_post("/tts", synthesize)
    runner, endpoint = await _start_server(app)
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")
    try:
        result = await PocketTtsProvider(f"{endpoint}/tts").synthesize(
            "Hello", blocked_parent / "reply.wav"
        )
    finally:
        await runner.cleanup()

    assert result.success is False
    assert result.error is not None
    assert "Failed to save" in result.error


@pytest.mark.asyncio
async def test_pocket_tts_health_reports_ready_and_unavailable():
    async def ready_handler(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(text="ready")

    app = web.Application()
    app.router.add_get("/", ready_handler)
    runner, endpoint = await _start_server(app)
    try:
        ready = await PocketTtsProvider(f"{endpoint}/tts").health()
    finally:
        await runner.cleanup()

    unavailable = await PocketTtsProvider("http://127.0.0.1:1/tts").health()

    assert ready.ready is True
    assert ready.endpoint == f"{endpoint}/"
    assert unavailable.ready is False
    assert unavailable.error is not None


@pytest.mark.asyncio
async def test_pocket_tts_health_reports_provider_failure():
    async def unavailable_handler(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(status=503)

    app = web.Application()
    app.router.add_get("/", unavailable_handler)
    runner, endpoint = await _start_server(app)
    try:
        health = await PocketTtsProvider(f"{endpoint}/tts").health()
    finally:
        await runner.cleanup()

    assert health.ready is False
    assert health.error == "Pocket TTS returned HTTP 503"


def test_pocket_tts_is_discovered_through_the_speech_plugin_hook():
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(PocketTtsPlugin())

    provider = get_speech_synthesizer(manager)

    assert provider is not None
    assert provider.name == "pocket-tts"


def test_speech_discovery_ignores_invalid_providers() -> None:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(_InvalidSpeechPlugin())

    assert get_speech_synthesizer(manager) is None


def test_speech_discovery_quarantines_plugin_hook_failures() -> None:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(_BrokenSpeechPlugin())

    assert get_speech_synthesizer(manager) is None


def test_speech_discovery_selects_the_first_valid_provider() -> None:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    first = _SpeechProvider("first")
    manager.register(_SpeechPlugin(_SpeechProvider("second")))
    manager.register(_SpeechPlugin(first))

    assert get_speech_synthesizer(manager) is first
