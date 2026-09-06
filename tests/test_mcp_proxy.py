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
    create_proxy_app,
)
from pynchy.host.container_manager.security.cop import CopVerdict
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


class TestMcpProxyRouting:
    async def test_proxy_forwards_to_backend(self, mock_backend):
        """Proxy should forward requests to the correct backend."""
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
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 200
        finally:
            await client.close()

    async def test_invalid_backend_json_is_returned_unchanged(self):
        async def handle(_request: web.Request) -> web.Response:  # noqa: RUF029 - aiohttp handler contract.
            return web.Response(body=b"not-json", content_type="text/plain")

        backend_app = web.Application()
        backend_app.router.add_route("*", "/mcp", handle)
        backend = TestServer(backend_app)
        await backend.start_server()
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")},
            services={"browser": ServiceTrustConfig(public_source=True, dangerous_writes=False)},
        )
        create_gate("test-ws", 1000.0, security)
        proxy = create_proxy_app(
            {"browser": f"http://localhost:{backend.port}/mcp"},
            trust_map={"browser": {"public_source": True}},
        )
        client = TestClient(TestServer(proxy))
        await client.start_server()

        try:
            response = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert response.status == 200
            assert await response.read() == b"not-json"
        finally:
            await client.close()
            await backend.close()

    async def test_proxy_uses_configured_service_for_hashed_instance(self, mock_backend):
        """Workspace-scoped instance IDs must retain their configured trust policy."""
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")}, services={"linear": _SAFE_TRUST}
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
            assert resp.status == 200
        finally:
            await client.close()

    @pytest.mark.parametrize("tool_name", ["linear_create_todo", "linear_archive_issue"])
    async def test_proxy_uses_configured_service_for_hashed_capability(
        self, mock_backend, tool_name
    ):
        """Capability rules target configured names, never generated instance IDs."""
        security = WorkspaceSecurity(
            services={"linear": _SAFE_TRUST},
            capabilities={
                f"mcp.linear.{tool_name}": CapabilityRule(decision="deny"),
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
                    "params": {"name": tool_name, "arguments": {}},
                },
            )
            assert resp.status == 403
            assert f"mcp.linear.{tool_name}" in (await resp.json())["error"]
        finally:
            await client.close()

    async def test_proxy_does_not_duplicate_streamable_http_mcp_path(self, mock_backend):
        """A streamable-HTTP client must not make the backend path ``/mcp/mcp``."""
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

    async def test_proxy_404_instance_not_assigned_to_workspace(self):
        """A workspace must not reach another workspace's MCP credentials."""
        create_gate(
            "other-ws",
            1000.0,
            WorkspaceSecurity(
                capabilities={"*": CapabilityRule("allow")}, services={"browser": _SAFE_TRUST}
            ),
        )
        app = create_proxy_app(
            {"browser": "http://localhost:9999/mcp"},
            authorize_instance=lambda workspace, instance: (
                workspace == "test-ws" and instance == "browser"
            ),
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            resp = await client.post("/mcp/other-ws/1000.0/browser", json={})
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
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")}, services={"browser": _SAFE_TRUST}
        )
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
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")}, services={"browser": _SAFE_TRUST}
        )
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
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")}, services={"browser": _SAFE_TRUST}
        )
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


class TestMcpProxyFencing:
    async def test_public_source_response_is_fenced(self, mock_backend):
        """Responses from public_source=true servers should be fenced."""
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")},
            services={
                "browser": ServiceTrustConfig(
                    public_source=True,
                    dangerous_writes=False,
                )
            },
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
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")}, services={"browser": _SAFE_TRUST}
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

    async def test_cop_flagged_content_is_blocked(self, mock_backend):
        """When Cop flags content, it should be replaced with a warning."""
        with patch(
            "pynchy.host.container_manager.mcp.proxy.inspect_inbound",
            new_callable=AsyncMock,
        ) as mock_cop:
            mock_cop.return_value = CopVerdict(flagged=True, reason="Prompt injection detected")

            security = WorkspaceSecurity(
                capabilities={"*": CapabilityRule("allow")},
                services={
                    "browser": ServiceTrustConfig(
                        public_source=True,
                        dangerous_writes=False,
                    )
                },
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
            capabilities={"*": CapabilityRule("allow")},
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

    async def test_non_text_content_is_returned_unchanged(self):
        """Fencing only rewrites text blocks in a public MCP result."""

        async def handle(_request: web.Request) -> web.Response:
            await asyncio.sleep(0)
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "image", "data": "abc"}]},
                }
            )

        backend_app = web.Application()
        backend_app.router.add_route("*", "/mcp", handle)
        backend = TestServer(backend_app)
        await backend.start_server()
        create_gate(
            "test-ws",
            1000.0,
            WorkspaceSecurity(
                capabilities={"*": CapabilityRule("allow")}, services={"browser": _SAFE_TRUST}
            ),
        )
        client = TestClient(
            TestServer(
                create_proxy_app(
                    {"browser": f"http://localhost:{backend.port}/mcp"},
                    trust_map={"browser": {"public_source": True}},
                )
            )
        )
        await client.start_server()

        try:
            response = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert response.status == 200
            assert (await response.json())["result"]["content"] == [
                {"type": "image", "data": "abc"}
            ]
        finally:
            await client.close()
            await backend.close()

    async def test_fencing_sets_corruption_taint(self, mock_backend):
        """Reading from a public_source server should set corruption taint on the gate."""
        security = WorkspaceSecurity(
            capabilities={"*": CapabilityRule("allow")},
            services={
                "browser": ServiceTrustConfig(
                    public_source=True,
                    dangerous_writes=False,
                )
            },
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
            assert not gate.corruption_tainted

            await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )

            assert gate.corruption_tainted
        finally:
            await client.close()
