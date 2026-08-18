"""Tests for the MCP proxy -- security enforcement for MCP traffic."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pynchy.host.container_manager.mcp.proxy import (
    McpProxy,
    create_proxy_app,
)
from pynchy.host.container_manager.security.approval import resolve_mcp_proxy_approval
from pynchy.host.container_manager.security.gate import create_gate
from pynchy.workspace.api import (
    CapabilityRule,
    ServiceTrustConfig,
    WorkspaceSecurity,
)

pytest_plugins = ("tests.mcp_proxy_support",)

# Fully safe trust config — passes outbound gating without triggering needs_human
_SAFE_TRUST = ServiceTrustConfig(
    public_source=False, secret_data=False, public_sink=False, dangerous_writes=False
)


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

    @pytest.mark.parametrize(("approved", "expected_status"), [(True, 200), (False, 403)])
    async def test_capability_wildcard_needs_human_can_be_decided(
        self, mock_backend, approved, expected_status
    ):
        """A wildcard MCP capability can require and enforce human approval."""
        security = WorkspaceSecurity(
            services={"email": _SAFE_TRUST},
            capabilities={"mcp.email.*": CapabilityRule(decision="needs_human")},
        )
        create_gate("test-ws", 1000.0, security)

        approval_calls: list[tuple[str, str, str]] = []

        def mock_approval_fn(group, tool_name, data, request_id):
            approval_calls.append((group, tool_name, request_id))
            resolve_mcp_proxy_approval(request_id, approved=approved)
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

            assert resp.status == expected_status
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
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")}, services={"browser": _SAFE_TRUST}
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
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")}, services={"browser": _SAFE_TRUST}
        )
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


class TestMcpProxyLifecycle:
    async def test_stop_before_start_is_idempotent(self):
        await McpProxy().stop()

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
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")}, services={"browser": _SAFE_TRUST}
        )
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
