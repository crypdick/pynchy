"""End-to-end test for MCP security enforcement.

Verifies the full flow: gate creation -> MCP proxy routing -> fencing ->
taint tracking -> gate cleanup. These are integration tests that exercise
the complete security pipeline through real aiohttp servers rather than
unit-testing individual components.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pynchy.security.cop import CopVerdict
from pynchy.security.gate import create_gate, destroy_gate, get_gate, _gates
from pynchy.types import ServiceTrustConfig, WorkspaceSecurity


@pytest.fixture(autouse=True)
def _cleanup_gates():
    """Ensure no gates leak between tests."""
    yield
    _gates.clear()


@pytest.fixture(autouse=True)
def _mock_cop():
    """Mock the Cop inspector to avoid real LLM API calls."""
    with patch(
        "pynchy.container_runner._mcp_proxy.inspect_inbound",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = CopVerdict(flagged=False)
        yield m


async def test_full_mcp_security_flow():
    """End-to-end: gate -> proxy -> fencing -> taint tracking -> cleanup."""
    from pynchy.container_runner._mcp_proxy import create_proxy_app

    # 1. Set up mock MCP backend
    async def backend_handler(request: web.Request) -> web.Response:
        return web.json_response({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "Hello from the web!"}],
            },
        })

    backend_app = web.Application()
    backend_app.router.add_route("*", "/mcp", backend_handler)
    backend_server = TestServer(backend_app)
    await backend_server.start_server()

    try:
        # 2. Create SecurityGate with browser as public_source
        security = WorkspaceSecurity(services={
            "browser": ServiceTrustConfig(
                public_source=True,
                secret_data=False,
                public_sink=False,
                dangerous_writes=False,
            ),
        })
        gate = create_gate("e2e-ws", 42.0, security)
        assert not gate.policy.corruption_tainted

        # 3. Create proxy pointing to backend
        proxy_app = create_proxy_app(
            {"browser": f"http://localhost:{backend_server.port}/mcp"},
            trust_map={"browser": {"public_source": True}},
        )
        proxy_client = TestClient(TestServer(proxy_app))
        await proxy_client.start_server()

        try:
            # 4. Make a request through the proxy
            resp = await proxy_client.post(
                "/mcp/e2e-ws/42.0/browser",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 200

            # 5. Response should be fenced with untrusted content markers
            data = await resp.json()
            text = data["result"]["content"][0]["text"]
            assert "EXTERNAL_UNTRUSTED_CONTENT" in text
            assert "Hello from the web!" in text

            # 6. Gate should now have corruption taint
            assert gate.policy.corruption_tainted

        finally:
            await proxy_client.close()

        # 7. Cleanup — destroy gate and verify it's gone
        destroy_gate("e2e-ws", 42.0)
        assert get_gate("e2e-ws", 42.0) is None

    finally:
        await backend_server.close()


async def test_no_fencing_without_public_source():
    """Non-public-source servers should pass through unfenced."""
    from pynchy.container_runner._mcp_proxy import create_proxy_app

    async def backend_handler(request: web.Request) -> web.Response:
        return web.json_response({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "Private data"}],
            },
        })

    backend_app = web.Application()
    backend_app.router.add_route("*", "/mcp", backend_handler)
    backend_server = TestServer(backend_app)
    await backend_server.start_server()

    try:
        security = WorkspaceSecurity(services={
            "notebook": ServiceTrustConfig(public_source=False),
        })
        gate = create_gate("e2e-ws", 42.0, security)

        proxy_app = create_proxy_app(
            {"notebook": f"http://localhost:{backend_server.port}/mcp"},
            trust_map={"notebook": {"public_source": False}},
        )
        proxy_client = TestClient(TestServer(proxy_app))
        await proxy_client.start_server()

        try:
            resp = await proxy_client.post(
                "/mcp/e2e-ws/42.0/notebook",
                json={"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            )
            assert resp.status == 200
            data = await resp.json()
            text = data["result"]["content"][0]["text"]
            assert "EXTERNAL_UNTRUSTED_CONTENT" not in text
            assert text == "Private data"
            assert not gate.policy.corruption_tainted
        finally:
            await proxy_client.close()
    finally:
        await backend_server.close()


async def test_gate_isolation_between_sessions():
    """Taint from one session shouldn't affect another."""
    security = WorkspaceSecurity(services={
        "browser": ServiceTrustConfig(public_source=True),
    })
    gate1 = create_gate("ws1", 1.0, security)
    gate2 = create_gate("ws2", 2.0, security)

    gate1.evaluate_read("browser")  # Taints gate1
    assert gate1.policy.corruption_tainted
    assert not gate2.policy.corruption_tainted  # gate2 unaffected
