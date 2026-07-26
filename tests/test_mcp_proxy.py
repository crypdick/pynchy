"""Tests for the MCP proxy -- security enforcement for MCP traffic."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pynchy.host.container_manager.mcp.proxy import (
    McpBackendUnavailableError,
    McpProxy,
    create_proxy_app,
)
from pynchy.host.container_manager.security.approval import resolve_mcp_proxy_approval
from pynchy.host.container_manager.security.cop import CopVerdict
from pynchy.host.container_manager.security.gate import create_gate, destroy_gate
from pynchy.types import CapabilityRule, ServiceTrustConfig, WorkspaceSecurity

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


# ---------------------------------------------------------------------------
# Routing tests
# ---------------------------------------------------------------------------


class TestMcpProxyRouting:
    async def test_proxy_forwards_to_backend(self, mock_backend):
        """Proxy should forward requests to the correct backend."""
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

    async def test_proxy_uses_configured_service_for_hashed_instance(self, mock_backend):
        """Workspace-scoped instance IDs must retain their configured trust policy."""
        security = WorkspaceSecurity(services={"linear": _SAFE_TRUST})
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app(
            {"linear_a1b2c3": backend_url},
            service_names={"linear_a1b2c3": "linear"},
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/linear_a1b2c3",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "linear_create_todo", "arguments": {}},
                },
            )
            assert resp.status == 200
        finally:
            await client.close()

    async def test_proxy_uses_configured_service_for_hashed_capability(self, mock_backend):
        """Capability rules target configured names, never generated instance IDs."""
        security = WorkspaceSecurity(
            services={"linear": _SAFE_TRUST},
            capabilities={
                "mcp.linear.linear_create_todo": CapabilityRule(decision="deny"),
            },
        )
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app(
            {"linear_a1b2c3": backend_url},
            service_names={"linear_a1b2c3": "linear"},
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/linear_a1b2c3",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "linear_create_todo", "arguments": {}},
                },
            )
            assert resp.status == 403
            assert "mcp.linear.linear_create_todo" in (await resp.json())["error"]
        finally:
            await client.close()

    async def test_proxy_does_not_duplicate_streamable_http_mcp_path(self, mock_backend):
        """A streamable-HTTP client must not make the backend path ``/mcp/mcp``."""
        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app({"browser": backend_url})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser/mcp",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 200
        finally:
            await client.close()

    async def test_proxy_404_unknown_instance(self):
        """Proxy should return 404 for unknown MCP instances."""
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
        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
        create_gate("test-ws", 1000.0, security)
        events: list[str] = []

        @asynccontextmanager
        async def backend_lease(_instance_id: str):
            events.append("enter")
            try:
                yield
            finally:
                events.append("release")

        # Port 1 is unlikely to be listening
        app = create_proxy_app(
            {"browser": "http://localhost:1/mcp"},
            backend_lease=backend_lease,
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 502
            assert events == ["enter", "release"]
        finally:
            await client.close()

    async def test_proxy_ensures_stopped_backend_before_forwarding(self):
        """A valid request should restart its managed backend before forwarding."""
        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
        create_gate("test-ws", 1000.0, security)
        backend_running = False
        events: list[str] = []

        async def handle(_request: web.Request) -> web.Response:
            await asyncio.sleep(0)
            assert backend_running
            events.append("forward")
            return web.json_response({"jsonrpc": "2.0", "id": 1, "result": {}})

        backend_app = web.Application()
        backend_app.router.add_post("/mcp", handle)
        backend = TestServer(backend_app)
        await backend.start_server()

        @asynccontextmanager
        async def backend_lease(instance_id: str):
            nonlocal backend_running
            await asyncio.sleep(0)
            assert instance_id == "browser"
            events.append("ensure")
            backend_running = True
            yield

        app = create_proxy_app(
            {"browser": f"http://localhost:{backend.port}/mcp"},
            backend_lease=backend_lease,
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )

            assert resp.status == 200
            assert events == ["ensure", "forward"]
        finally:
            await client.close()
            await backend.close()

    async def test_proxy_returns_502_when_backend_ensure_fails(self, mock_backend):
        """A managed-backend startup failure should not escape as a proxy 500."""
        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
        create_gate("test-ws", 1000.0, security)
        ensure_backend = AsyncMock(side_effect=RuntimeError("start failed"))

        @asynccontextmanager
        async def backend_lease(instance_id: str):
            try:
                await ensure_backend(instance_id)
            except RuntimeError as exc:
                raise McpBackendUnavailableError(instance_id) from exc
            yield

        app = create_proxy_app(
            {"browser": f"http://localhost:{mock_backend.port}/mcp"},
            backend_lease=backend_lease,
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )

            assert resp.status == 502
            assert await resp.json() == {"error": "MCP backend unavailable"}
            ensure_backend.assert_awaited_once_with("browser")
        finally:
            await client.close()

    async def test_proxy_400_invalid_invocation_ts(self):
        """Proxy should return 400 for non-numeric invocation_ts."""
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

    async def test_cop_flagged_content_is_blocked(self, mock_backend):
        """When Cop flags content, it should be replaced with a warning."""
        with patch(
            "pynchy.host.container_manager.mcp.proxy.inspect_inbound",
            new_callable=AsyncMock,
        ) as mock_cop:
            mock_cop.return_value = CopVerdict(flagged=True, reason="Prompt injection detected")

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

    async def test_inactive_cop_fences_without_inspection(self, mock_backend):
        """Unattended profiles retain provenance fencing without Cop calls."""
        security = WorkspaceSecurity(
            services={
                "browser": ServiceTrustConfig(
                    public_source=True,
                    dangerous_writes=False,
                )
            },
            cop_active=False,
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
            with patch(
                "pynchy.host.container_manager.mcp.proxy.inspect_inbound",
                new_callable=AsyncMock,
            ) as inspect:
                resp = await client.post(
                    "/mcp/test-ws/1000.0/browser",
                    json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
                )

            assert resp.status == 200
            data = await resp.json()
            text = data["result"]["content"][0]["text"]
            assert "EXTERNAL_UNTRUSTED_CONTENT" in text
            inspect.assert_not_awaited()
        finally:
            await client.close()

    async def test_fencing_sets_corruption_taint(self, mock_backend):
        """Reading from a public_source server should set corruption taint on the gate."""
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
        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(dangerous_writes="forbidden")}
        )
        create_gate("test-ws", 1000.0, security)
        backend_lease = AsyncMock()

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app(
            {"browser": backend_url},
            backend_lease=backend_lease,
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
                    "params": {"name": "browser_type", "arguments": {"text": "secret"}},
                },
            )
            assert resp.status == 403
            data = await resp.json()
            assert "error" in data
            assert "denied" in data["error"].lower() or "forbidden" in data["error"].lower()
            backend_lease.assert_not_called()
        finally:
            await client.close()

    async def test_capability_deny_blocks_specific_tool_before_backend(self, mock_backend):
        """A denied MCP capability should not reach the backend."""
        security = WorkspaceSecurity(
            services={"email": _SAFE_TRUST},
            capabilities={"mcp.email.send": CapabilityRule(decision="deny")},
        )
        create_gate("test-ws", 1000.0, security)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app({"email": backend_url})
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/email",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "send", "arguments": {"to": "a@example.com"}},
                },
            )

            assert resp.status == 403
            data = await resp.json()
            assert "capability" in data["error"].lower()
            assert "mcp.email.send" in data["error"]
        finally:
            await client.close()

    async def test_capability_allow_skips_service_human_gate(self, mock_backend):
        """An explicit profile allow should not prompt for a dangerous service write."""
        security = WorkspaceSecurity(
            services={"email": ServiceTrustConfig(dangerous_writes=True)},
            capabilities={"mcp.email.send": CapabilityRule(decision="allow")},
        )
        create_gate("test-ws", 1000.0, security)
        approval_fn = AsyncMock()

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app({"email": backend_url}, approval_fn=approval_fn)
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/email",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "send", "arguments": {"to": "a@example.com"}},
                },
            )

            assert resp.status == 200
            approval_fn.assert_not_awaited()
        finally:
            await client.close()

    async def test_capability_wildcard_needs_human_can_be_approved(self, mock_backend):
        """A wildcard MCP capability can require human approval."""
        security = WorkspaceSecurity(
            services={"email": _SAFE_TRUST},
            capabilities={"mcp.email.*": CapabilityRule(decision="needs_human")},
        )
        create_gate("test-ws", 1000.0, security)

        approval_calls: list[tuple[str, str, str]] = []

        def mock_approval_fn(group, tool_name, data, request_id):
            approval_calls.append((group, tool_name, request_id))
            resolve_mcp_proxy_approval(request_id, approved=True)
            return asyncio.sleep(0)

        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        app = create_proxy_app({"email": backend_url}, approval_fn=mock_approval_fn)
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/email",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "send", "arguments": {"to": "a@example.com"}},
                },
            )

            assert resp.status == 200
            assert approval_calls == [("test-ws", "send", approval_calls[0][2])]
        finally:
            await client.close()

    async def test_needs_human_blocks_and_approves(self, mock_backend):
        """A tools/call that needs_human should block until human approves."""
        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(dangerous_writes=True)}
        )
        create_gate("test-ws", 1000.0, security)

        approval_calls: list[tuple] = []

        def mock_approval_fn(group, tool_name, data, request_id):
            approval_calls.append((group, tool_name, request_id))
            # Simulate immediate human approval
            resolve_mcp_proxy_approval(request_id, approved=True)
            return asyncio.sleep(0)

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
        security = WorkspaceSecurity(
            services={"browser": ServiceTrustConfig(dangerous_writes=True)}
        )
        create_gate("test-ws", 1000.0, security)

        def mock_approval_fn(group, tool_name, data, request_id):
            # Simulate human denial
            resolve_mcp_proxy_approval(request_id, approved=False)
            return asyncio.sleep(0)

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
        proxy = McpProxy()
        port = await proxy.start({})
        assert port > 0
        assert proxy.port == port
        await proxy.stop()

    async def test_routes_resolve_from_instance_map(self, mock_backend):
        """An instance 404s when unmapped and 200s when its route is configured.

        Uses TestClient (in-process) instead of real TCP to avoid
        port-binding issues under pytest-xdist workers.
        """
        security = WorkspaceSecurity(services={"browser": _SAFE_TRUST})
        create_gate("test-ws", 1000.0, security)

        # No routes configured -- should 404
        app = create_proxy_app({})
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 404
        finally:
            await client.close()

        # Route configured -- should succeed
        backend_url = f"http://localhost:{mock_backend.port}/mcp"
        mapped_app = create_proxy_app({"browser": backend_url})
        mapped_client = TestClient(TestServer(mapped_app))
        await mapped_client.start_server()
        try:
            resp = await mapped_client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 200
        finally:
            await mapped_client.close()
