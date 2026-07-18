"""Tests for local Pocket TTS synthesis."""

from __future__ import annotations

import pytest
from aiohttp import web

from pynchy.host import audio


@pytest.mark.asyncio
async def test_synthesis_posts_text_to_local_pocket_tts(tmp_path, monkeypatch):
    received: dict[str, str] = {}

    async def synthesize(request: web.Request) -> web.Response:
        form = await request.post()
        received["text"] = str(form["text"])
        return web.Response(body=b"audio", content_type="audio/wav")

    app = web.Application()
    app.router.add_post("/tts", synthesize)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        server = site._server
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(audio, "POCKET_TTS_ENDPOINT", f"http://127.0.0.1:{port}/tts")

        output_path = tmp_path / "reply.wav"
        result = await audio.synthesize_speech_to_file("Hello", output_path)
    finally:
        await runner.cleanup()

    assert received == {"text": "Hello"}
    assert result.success is True
    assert result.output_path == output_path
    assert output_path.read_bytes() == b"audio"
