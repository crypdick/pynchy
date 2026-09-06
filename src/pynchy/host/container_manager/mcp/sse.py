"""Incremental forwarding for MCP server-sent events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress

import aiohttp
from aiohttp import web

SseDataTransformer = Callable[[bytes], Awaitable[bytes]]


async def _transform_event(event: bytes, transform_data: SseDataTransformer) -> bytes:
    separator = b"\r\n" if b"\r\n" in event else b"\n"
    transformed: list[bytes] = []
    for line in event.rstrip(b"\r\n").split(separator):
        if line.startswith(b"data:"):
            transformed.append(b"data: " + await transform_data(line[5:].lstrip()))
        else:
            transformed.append(line)
    return separator.join(transformed) + separator * 2


async def stream_response(
    request: web.Request,
    backend_response: aiohttp.ClientResponse,
    transform_data: SseDataTransformer,
) -> web.StreamResponse:
    """Forward each complete SSE event without buffering the open stream."""
    response = web.StreamResponse(
        status=backend_response.status,
        headers={
            key: value
            for key, value in backend_response.headers.items()
            if key.lower() not in ("content-length", "transfer-encoding")
        },
    )
    # A client may close its SSE listener before deleting the MCP session.
    # Only downstream resets are normal teardown; upstream read errors propagate.
    with suppress(aiohttp.ClientConnectionResetError):
        await response.prepare(request)
        event = bytearray()
        async for line in backend_response.content:
            event.extend(line)
            if line not in (b"\n", b"\r\n"):
                continue
            await response.write(await _transform_event(bytes(event), transform_data))
            event.clear()
        if event:
            await response.write(await _transform_event(bytes(event), transform_data))
        await response.write_eof()
    return response
