"""Tests for the MCP proxy -- security enforcement for MCP traffic."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pynchy.host.container_manager.security.approval import (
    _mcp_proxy_futures,
    resolve_mcp_proxy_approval,
)
from pynchy.host.container_manager.security.cop import CopVerdict
from pynchy.host.container_manager.security.gate import _gates, create_gate
from pynchy.types import ServiceTrustConfig, WorkspaceSecurity

# Fully safe trust config — passes outbound gating without triggering needs_human
_SAFE_TRUST = ServiceTrustConfig(
    public_source=False, secret_data=False, public_sink=False, dangerous_writes=False
)


@pytest.fixture(autouse=True)
def _cleanup_gates():
    """Ensure no gates or approval futures leak between tests."""
    yield
    _gates.clear()
    _mcp_proxy_futures.clear()


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
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
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
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

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
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

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
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
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
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

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
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={
                "browser": ServiceTrustConfig(
                    public_source=True,
                    dangerous_writes=False,
                )
            }
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
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
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
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        _mock_cop.return_value = CopVerdict(flagged=True, reason="Prompt injection detected")

        security = WorkspaceSecurity(
            services={
                "browser": ServiceTrustConfig(
                    public_source=True,
                    dangerous_writes=False,
                )
            }
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
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={
                "browser": ServiceTrustConfig(
                    public_source=True,
                    dangerous_writes=False,
                )
            }
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
# Outbound gating tests
# ---------------------------------------------------------------------------


class TestMcpProxyOutboundGating:
    """Tests for outbound (request-side) SecurityGate enforcement.

    The proxy should evaluate_write() on MCP tools/call requests before
    forwarding to the backend. Forbidden tools are denied, dangerous tools
    requiring human approval are denied with an informative error.
    """

    async def test_forbidden_write_denied(self, mock_backend):
        """A tools/call to a service with dangerous_writes=forbidden should be denied."""
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(dangerous_writes="forbidden")}
        )
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app({"browser": backend_url})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "browser_type", "arguments": {"text": "secret"}},
                },
            )
            assert resp.status == 403
            data = await resp.json()
            assert "error" in data
            assert "denied" in data["error"].lower() or "forbidden" in data["error"].lower()
        finally:
            await client.close()

    async def test_needs_human_blocks_and_approves(self, mock_backend):
        """A tools/call that needs_human should block until human approves."""
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(dangerous_writes=True)}
        )
        create_gate("test-ws", 1000.0, security)

        approval_calls: list[tuple] = []

        async def mock_approval_fn(group, tool_name, data, request_id):
            approval_calls.append((group, tool_name, request_id))
            # Simulate immediate human approval
            resolve_mcp_proxy_approval(request_id, True)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app(
            {"browser": backend_url},
            approval_fn=mock_approval_fn,
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "browser_type", "arguments": {"text": "data"}},
                },
            )
            # Approved — request should be forwarded to backend
            assert resp.status == 200
            assert len(approval_calls) == 1
            assert approval_calls[0][0] == "test-ws"
            assert approval_calls[0][1] == "browser_type"
        finally:
            await client.close()

    async def test_needs_human_blocks_and_denies(self, mock_backend):
        """A tools/call denied by human should return 403."""
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(dangerous_writes=True)}
        )
        create_gate("test-ws", 1000.0, security)

        async def mock_approval_fn(group, tool_name, data, request_id):
            # Simulate human denial
            resolve_mcp_proxy_approval(request_id, False)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app(
            {"browser": backend_url},
            approval_fn=mock_approval_fn,
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "browser_type", "arguments": {}},
                },
            )
            assert resp.status == 403
            data = await resp.json()
            assert "denied" in data["error"].lower()
        finally:
            await client.close()

    async def test_needs_human_no_approval_fn_returns_403(self, mock_backend):
        """Without an approval_fn, needs_human should return 403 immediately."""
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(dangerous_writes=True)}
        )
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        # No approval_fn provided
        app = create_proxy_app({"browser": backend_url})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "browser_type", "arguments": {}},
                },
            )
            assert resp.status == 403
            data = await resp.json()
            assert "approval" in data["error"].lower()
        finally:
            await client.close()

    async def test_safe_write_allowed_through(self, mock_backend):
        """A tools/call to a fully-safe service should pass through."""
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app({"browser": backend_url})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "browser_click", "arguments": {}},
                },
            )
            assert resp.status == 200
        finally:
            await client.close()

    async def test_non_tools_call_not_gated(self, mock_backend):
        """Non-tools/call MCP methods (e.g. resources/read) should not be write-gated."""
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(dangerous_writes="forbidden")}
        )
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app({"browser": backend_url})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={
                    "jsonrpc": "2.0",
                    "method": "resources/read",
                    "id": 1,
                    "params": {"uri": "file:///tmp/test"},
                },
            )
            assert resp.status == 200
        finally:
            await client.close()

    async def test_malformed_json_body_passes_through(self, mock_backend):
        """Non-JSON request bodies should be forwarded without write gating."""
        from pynchy.host.container_manager.mcp.proxy import create_proxy_app

        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app({"browser": backend_url})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                data=b"not json",
            )
            assert resp.status == 200
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# McpProxy lifecycle tests
# ---------------------------------------------------------------------------


class TestMcpProxyLifecycle:
    async def test_start_and_stop(self):
        """McpProxy should start on a dynamic port and stop cleanly."""
        from pynchy.host.container_manager.mcp.proxy import McpProxy

        proxy = McpProxy()
        port = await proxy.start({})
        assert port > 0
        assert proxy.port == port
        await proxy.stop()

    async def test_update_routes(self, mock_backend):
        """update_routes should update the instance URL mapping.

        Uses TestClient (in-process) instead of real TCP to avoid
        port-binding issues under pytest-xdist workers.
        """
        from pynchy.host.container_manager.mcp.proxy import _STATE_KEY, create_proxy_app

        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
        create_gate("test-ws", 1000.0, security)

        # Start with empty routes via the app directly (TestClient, no real TCP)
        app = create_proxy_app({})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            # Initially no routes -- should 404
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 404

            # Mutate routes via _ProxyState (same mechanism as McpProxy.update_routes)
            backend_url = f"http://localhost:{mock_backend.port}/mcp"
            app[_STATE_KEY].instance_urls = {"browser": backend_url}

            # Now should succeed
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 200
        finally:
            await client.close()
