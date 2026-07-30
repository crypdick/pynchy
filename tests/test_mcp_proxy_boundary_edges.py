"""Boundary coverage for MCP proxy forwarding and approval timeouts."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from pynchy.host.container_manager.mcp.proxy import create_proxy_app
from pynchy.host.container_manager.security.approval import resolve_mcp_proxy_approval
from pynchy.host.container_manager.security.gate import create_gate
from pynchy.workspace.api import ServiceTrustConfig, WorkspaceSecurity

pytest_plugins = ("tests.mcp_proxy_support",)

_SAFE_TRUST = ServiceTrustConfig(
    public_source=False,
    secret_data=False,
    public_sink=False,
    dangerous_writes=False,
)


async def test_proxy_appends_missing_backend_path_tail(mock_backend) -> None:
    create_gate("test-ws", 1000.0, WorkspaceSecurity(services={"browser": _SAFE_TRUST}))
    app = create_proxy_app({"browser": f"http://localhost:{mock_backend.port}"})
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/mcp/test-ws/1000.0/browser/mcp",
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )
        assert response.status == 200
    finally:
        await client.close()


async def test_proxy_returns_timeout_when_human_approval_does_not_resolve(mock_backend) -> None:
    create_gate(
        "test-ws",
        1000.0,
        WorkspaceSecurity(services={"browser": ServiceTrustConfig(dangerous_writes=True)}),
    )
    approval = AsyncMock()
    app = create_proxy_app(
        {"browser": f"http://localhost:{mock_backend.port}/mcp"},
        approval_fn=approval,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        with patch(
            "pynchy.host.container_manager.mcp.proxy.APPROVAL_TIMEOUT_SECONDS",
            0.0,
        ):
            response = await client.post(
                "/mcp/test-ws/1000.0/browser",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "browser_type", "arguments": {}},
                },
            )
        assert response.status == 408
        assert (await response.json())["error"] == "Human approval timed out"
        approval.assert_awaited_once()
    finally:
        request_id = approval.await_args.args[3]
        resolve_mcp_proxy_approval(request_id, approved=False)
        await client.close()
