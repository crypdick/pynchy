"""Tests for the MCP proxy -- security enforcement for MCP traffic."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from pynchy.host.container_manager.security.cop import CopVerdict
from pynchy.host.container_manager.security.gate import destroy_gate
from pynchy.workspace.api import (
    ServiceTrustConfig,
)

# Fully safe trust config — passes outbound gating without triggering needs_human
_SAFE_TRUST = ServiceTrustConfig(
    public_source=False, secret_data=False, public_sink=False, dangerous_writes=False
)


@pytest.fixture(autouse=True)
def _cleanup_gates():
    """Ensure the proxy gate is removed through the public lifecycle API."""
    yield
    destroy_gate("test-ws", 1000.0)


@pytest.fixture(autouse=True)
def _mock_cop():
    """Mock the Cop inspector so tests don't call the real LLM."""
    with patch(
        "pynchy.host.container_manager.mcp.proxy.inspect_inbound",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = CopVerdict(flagged=False)
        yield m


@pytest.fixture
async def mock_backend():
    """Start a mock MCP backend that echoes requests."""

    async def handle(request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "Page content from browser"}],
                },
            }
        )

    app = web.Application()
    app.router.add_route("*", "/mcp", handle)
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()
