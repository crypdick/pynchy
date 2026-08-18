"""Boundary coverage for MCP proxy forwarding and approval timeouts."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import pynchy.host.container_manager.mcp.proxy as proxy_module
from pynchy.host.container_manager.mcp.proxy import McpProxy, create_proxy_app
from pynchy.host.container_manager.security.approval import resolve_mcp_proxy_approval
from pynchy.host.container_manager.security.gate import create_gate
from pynchy.workspace.api import CapabilityRule, ServiceTrustConfig, WorkspaceSecurity

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


async def test_proxy_hides_denied_tools_and_still_blocks_direct_calls() -> None:
    async def handle(request: web.Request) -> web.Response:
        payload = await request.json()
        if payload["method"] == "tools/list":
            result = {"tools": [{"name": "read"}, {"name": "delete"}]}
        else:
            result = {"content": []}
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": result})

    backend = TestServer(web.Application())
    backend.app.router.add_post("/mcp", handle)
    await backend.start_server()
    create_gate(
        "test-ws",
        1000.0,
        WorkspaceSecurity(
            services={"browser": _SAFE_TRUST},
            capabilities={
                "mcp.browser.*": CapabilityRule("allow"),
                "mcp.browser.delete": CapabilityRule("deny"),
            },
        ),
    )
    app = create_proxy_app(
        {"browser-instance": f"http://localhost:{backend.port}/mcp"},
        service_names={"browser-instance": "browser"},
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        listed = await client.post(
            "/mcp/test-ws/1000.0/browser-instance",
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )
        called = await client.post(
            "/mcp/test-ws/1000.0/browser-instance",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 2,
                "params": {"name": "delete", "arguments": {}},
            },
        )

        assert [tool["name"] for tool in (await listed.json())["result"]["tools"]] == ["read"]
        assert called.status == 403
    finally:
        await client.close()
        await backend.close()


async def test_proxy_hides_denied_tools_from_event_stream_lists() -> None:
    async def handle(_request: web.Request) -> web.Response:
        await _request.read()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": "read"}, {"name": "delete"}]},
        }
        return web.Response(
            body=f"event: ping\ndata: not-json\nevent: message\ndata: {json.dumps(payload)}\n\n",
            content_type="text/event-stream",
        )

    backend = TestServer(web.Application())
    backend.app.router.add_post("/mcp", handle)
    await backend.start_server()
    create_gate(
        "test-ws",
        1000.0,
        WorkspaceSecurity(capabilities={"mcp.browser.delete": CapabilityRule("deny")}),
    )
    app = create_proxy_app(
        {"browser-instance": f"http://localhost:{backend.port}/mcp"},
        service_names={"browser-instance": "browser"},
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/mcp/test-ws/1000.0/browser-instance",
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )
        assert '"name": "read"' in await response.text()
        assert '"name": "delete"' not in await response.text()
    finally:
        await client.close()
        await backend.close()


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


async def test_proxy_app_cleanup_is_idempotent() -> None:
    app = create_proxy_app({})
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.close()
    await app.cleanup()


async def test_proxy_start_rejects_runner_without_a_bound_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runner(proxy_module.web.AppRunner):
        addresses: tuple[object, ...] = ()

        def __init__(self, _app: object) -> None:
            pass

        async def setup(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    class _Site:
        def __init__(self, _runner: _Runner, _host: str, _port: int) -> None:
            pass

        async def start(self) -> None:
            return None

    monkeypatch.setattr(proxy_module.web, "AppRunner", _Runner)
    monkeypatch.setattr(proxy_module.web, "TCPSite", _Site)

    with pytest.raises(RuntimeError, match="did not bind any addresses"):
        await McpProxy().start({})


async def test_proxy_fails_closed_when_http_session_was_not_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_gate("test-ws", 1000.0, WorkspaceSecurity())
    startup = AsyncMock()
    monkeypatch.setattr(proxy_module, "_start_http_session", startup)
    app = create_proxy_app({"browser": "http://backend.test/mcp"})
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/mcp/test-ws/1000.0/browser",
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )
        assert response.status == 500
    finally:
        await client.close()

    startup.assert_awaited_once()
