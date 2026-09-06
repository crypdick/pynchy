"""Client SSE teardown must release backend leases without backend-error reports."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from pynchy.host.container_manager.mcp.proxy import create_proxy_app
from pynchy.host.container_manager.security.gate import create_gate
from pynchy.workspace.api import WorkspaceSecurity

pytest_plugins = ("tests.mcp_proxy_support",)


@pytest.mark.parametrize("last_event", [b"", b"data: done\n\n", b"data: done", None])
async def test_sse_teardown_distinguishes_client_disconnect_from_backend_failure(last_event):
    release_backend = asyncio.Event()
    lease_released = asyncio.Event()
    requests: list[web.Request] = []

    @web.middleware
    async def observe_request(request, handler):
        requests.append(request)
        return await handler(request)

    @asynccontextmanager
    async def lease(instance_id):
        try:
            yield
        finally:
            lease_released.set()

    async def handle(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b": connected\n\n")
        await release_backend.wait()
        if last_event is None:
            assert request.transport is not None
            request.transport.abort()
        elif last_event:
            await response.write(last_event)
        return response

    backend = TestServer(web.Application())
    backend.app.router.add_get("/mcp", handle)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(backend)
        create_gate("test-ws", 1000.0, WorkspaceSecurity())
        app = create_proxy_app({"browser": str(backend.make_url("/mcp"))}, backend_lease=lease)
        app.middlewares.append(observe_request)
        # TestServer cancels disconnected handlers; production AppRunner does not.
        runner = web.AppRunner(app)
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        stack.callback(release_backend.set)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        client = await stack.enter_async_context(aiohttp.ClientSession())
        with patch("pynchy.host.container_manager.mcp.proxy.logger.error") as backend_error:
            response = await client.get(f"http://127.0.0.1:{port}/mcp/test-ws/1000.0/browser")
            assert await response.content.readuntil(b"\n\n") == b": connected\n\n"
            if last_event is not None:
                response.close()
                async with asyncio.timeout(1):
                    while requests[0].transport is not None:  # noqa: ASYNC110 - aiohttp exposes no disconnect event.
                        await asyncio.sleep(0)
            release_backend.set()
            await asyncio.wait_for(lease_released.wait(), 1)
            if last_event is None:
                backend_error.assert_called_once()
                assert backend_error.call_args.args == ("MCP proxy backend error",)
            else:
                backend_error.assert_not_called()
