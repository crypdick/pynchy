"""Tests for the MCP proxy -- security enforcement for MCP traffic."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pynchy.security.cop import CopVerdict
from pynchy.security.gate import create_gate, _gates
from pynchy.types import ServiceTrustConfig, WorkspaceSecurity


@pytest.fixture(autouse=True)
def _cleanup_gates():
    """Ensure no gates leak between tests."""
    yield
    _gates.clear()


@pytest.fixture(autouse=True)
def _mock_cop():
    """Mock the Cop inspector so tests don't call the real LLM."""
    with patch(
        "pynchy.container_runner._mcp_proxy.inspect_inbound",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = CopVerdict(flagged=False)
        yield m


@pytest.fixture
async def mock_backend():
    """Start a mock MCP backend that echoes requests."""

    async def handle(request: web.Request) -> web.Response:
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


# ---------------------------------------------------------------------------
# Routing tests
# ---------------------------------------------------------------------------


class TestMcpProxyRouting:
    async def test_proxy_forwards_to_backend(self, mock_backend):
        """Proxy should forward requests to the correct backend."""
        from pynchy.container_runner._mcp_proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(public_source=False)}
        )
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app({"browser": backend_url})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 200
        finally:
            await client.close()

    async def test_proxy_404_unknown_instance(self):
        """Proxy should return 404 for unknown MCP instances."""
        from pynchy.container_runner._mcp_proxy import create_proxy_app

        app = create_proxy_app({})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post("/mcp/test-ws/1000.0/nonexistent", json={})
            assert resp.status == 404
        finally:
            await client.close()

    async def test_proxy_403_no_gate(self):
        """Proxy should return 403 when no SecurityGate exists for the session."""
        from pynchy.container_runner._mcp_proxy import create_proxy_app

        app = create_proxy_app({"browser": "http://localhost:9999/mcp"})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post("/mcp/no-gate-ws/1000.0/browser", json={})
            assert resp.status == 403
        finally:
            await client.close()

    async def test_proxy_502_backend_unavailable(self):
        """Proxy should return 502 when the backend is unreachable."""
        from pynchy.container_runner._mcp_proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(public_source=False)}
        )
        create_gate("test-ws", 1000.0, security)

        # Port 1 is unlikely to be listening
        app = create_proxy_app({"browser": "http://localhost:1/mcp"})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 502
        finally:
            await client.close()

    async def test_proxy_400_invalid_invocation_ts(self):
        """Proxy should return 400 for non-numeric invocation_ts."""
        from pynchy.container_runner._mcp_proxy import create_proxy_app

        app = create_proxy_app({"browser": "http://localhost:9999/mcp"})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post("/mcp/test-ws/not-a-number/browser", json={})
            assert resp.status == 400
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Fencing tests
# ---------------------------------------------------------------------------


class TestMcpProxyFencing:
    async def test_public_source_response_is_fenced(self, mock_backend):
        """Responses from public_source=true servers should be fenced."""
        from pynchy.container_runner._mcp_proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(public_source=True)}
        )
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app(
            {"browser": backend_url},
            trust_map={"browser": {"public_source": True}},
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 200
            data = await resp.json()
            text = data["result"]["content"][0]["text"]
            assert "EXTERNAL_UNTRUSTED_CONTENT" in text
            assert "Page content from browser" in text
        finally:
            await client.close()

    async def test_non_public_source_not_fenced(self, mock_backend):
        """Responses from non-public_source servers should NOT be fenced."""
        from pynchy.container_runner._mcp_proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(public_source=False)}
        )
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app(
            {"browser": backend_url},
            trust_map={"browser": {"public_source": False}},
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 200
            data = await resp.json()
            text = data["result"]["content"][0]["text"]
            assert "EXTERNAL_UNTRUSTED_CONTENT" not in text
        finally:
            await client.close()

    async def test_cop_flagged_content_is_blocked(self, mock_backend, _mock_cop):
        """When Cop flags content, it should be replaced with a warning."""
        from pynchy.container_runner._mcp_proxy import create_proxy_app

        _mock_cop.return_value = CopVerdict(
            flagged=True, reason="Prompt injection detected"
        )

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(public_source=True)}
        )
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app(
            {"browser": backend_url},
            trust_map={"browser": {"public_source": True}},
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 200
            data = await resp.json()
            text = data["result"]["content"][0]["text"]
            assert "blocked by security policy" in text.lower()
            assert "Page content from browser" not in text
        finally:
            await client.close()

    async def test_fencing_sets_corruption_taint(self, mock_backend):
        """Reading from a public_source server should set corruption taint on the gate."""
        from pynchy.container_runner._mcp_proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(public_source=True)}
        )
        gate = create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app(
            {"browser": backend_url},
            trust_map={"browser": {"public_source": True}},
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            assert not gate.policy.corruption_tainted

            await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )

            assert gate.policy.corruption_tainted
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# McpProxy lifecycle tests
# ---------------------------------------------------------------------------


class TestMcpProxyLifecycle:
    async def test_start_and_stop(self):
        """McpProxy should start on a dynamic port and stop cleanly."""
        from pynchy.container_runner._mcp_proxy import McpProxy

        proxy = McpProxy()
        port = await proxy.start({})
        assert port > 0
        assert proxy.port == port
        await proxy.stop()

    async def test_update_routes(self, mock_backend):
        """update_routes should update the instance URL mapping."""
        from pynchy.container_runner._mcp_proxy import McpProxy

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(public_source=False)}
        )
        create_gate("test-ws", 1000.0, security)

        proxy = McpProxy()
        port = await proxy.start({})

        try:
            # Initially no routes -- should 404
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"http://localhost:{port}/mcp/test-ws/1000.0/browser",
                    json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
                )
                assert resp.status == 404

            # Add route
            backend_url = f"http://localhost:{mock_backend.port}/mcp"
            proxy.update_routes({"browser": backend_url})

            # Now should succeed
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"http://localhost:{port}/mcp/test-ws/1000.0/browser",
                    json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
                )
                assert resp.status == 200
        finally:
            await proxy.stop()
