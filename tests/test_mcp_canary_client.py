"""Protocol tests for the small MCP client used by operational canaries."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pynchy.host.container_manager.mcp.canary_client import (
    McpCanaryClient,
    McpCanaryClientError,
    McpCanaryToolError,
)


async def _start_server() -> tuple[TestClient, list[tuple[str, str | None]]]:
    calls: list[tuple[str, str | None]] = []

    async def handle(request: web.Request) -> web.Response:
        payload = await request.json()
        method = payload["method"]
        calls.append((method, request.headers.get("Mcp-Session-Id")))
        if method == "initialize":
            return web.json_response(
                {"jsonrpc": "2.0", "id": payload["id"], "result": {"capabilities": {}}},
                headers={"Mcp-Session-Id": "canary-session"},
            )
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "tools/list":
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"tools": [{"name": "gdrive_search"}]},
                }
            )
            return web.Response(text=f"data: {body}\n\n", content_type="text/event-stream")
        if method == "tools/call":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"isError": payload["params"]["name"] == "broken"},
                }
            )
        raise AssertionError(f"Unexpected MCP method: {method}")

    app = web.Application()
    app.router.add_post("/mcp", handle)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, calls


async def _start_response_server(body: str, *, status: int = 200) -> TestClient:
    def handle(_request: web.Request) -> web.Response:
        return web.Response(status=status, text=body, content_type="application/json")

    app = web.Application()
    app.router.add_post("/mcp", handle)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_mcp_canary_client_initializes_session_and_decodes_sse_tool_list():
    server, calls = await _start_server()
    try:
        async with McpCanaryClient(str(server.make_url("/mcp"))) as client:
            assert await client.list_tool_names() == {"gdrive_search"}
            assert await client.call_tool("gdrive_search", {"query": "fixture"}) == {
                "isError": False
            }
        assert calls == [
            ("initialize", None),
            ("notifications/initialized", "canary-session"),
            ("tools/list", "canary-session"),
            ("tools/call", "canary-session"),
        ]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_mcp_canary_client_rejects_provider_error_tool_results():
    server, _calls = await _start_server()
    try:
        async with McpCanaryClient(str(server.make_url("/mcp"))) as client:
            with pytest.raises(McpCanaryToolError):
                await client.call_tool("broken", {})
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_mcp_canary_client_rejects_missing_or_empty_tool_lists() -> None:
    client = McpCanaryClient("http://unused/mcp")
    for result, message in (({"tools": None}, "no tool list"), ({"tools": []}, "no callable")):
        with (
            patch.object(client, "_request", new=AsyncMock(return_value=result)),
            pytest.raises(McpCanaryClientError, match=message),
        ):
            await client.list_tool_names()


@pytest.mark.asyncio
async def test_mcp_canary_client_rejects_malformed_rpc_results() -> None:
    client = McpCanaryClient("http://unused/mcp")
    with (
        patch.object(client, "_post", new=AsyncMock(return_value=None)),
        pytest.raises(McpCanaryClientError, match="no response"),
    ):
        await client.call_tool("broken", {})

    with (
        patch.object(client, "_post", new=AsyncMock(return_value={"error": {}})),
        pytest.raises(McpCanaryToolError, match="rejected the request"),
    ):
        await client.call_tool("broken", {})

    with (
        patch.object(client, "_post", new=AsyncMock(return_value={"result": []})),
        pytest.raises(McpCanaryClientError, match="non-object result"),
    ):
        await client.call_tool("broken", {})


@pytest.mark.asyncio
async def test_mcp_canary_client_rejects_use_outside_its_context() -> None:
    with pytest.raises(McpCanaryClientError, match="outside its context"):
        await McpCanaryClient("http://unused/mcp").list_tool_names()


@pytest.mark.asyncio
async def test_mcp_canary_client_reports_transport_failure() -> None:
    with pytest.raises(McpCanaryClientError, match="server is unavailable"):
        async with McpCanaryClient("http://127.0.0.1:1/mcp"):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        ("{}", 500, "HTTP error"),
        ("[]", 200, "non-object response"),
        ("not-json", 200, "invalid response"),
        ("data: not-json\n\n", 200, "invalid SSE JSON"),
    ],
)
async def test_mcp_canary_client_rejects_invalid_http_and_payloads(
    body: str,
    status: int,
    message: str,
) -> None:
    server = await _start_response_server(body, status=status)
    try:
        with pytest.raises(McpCanaryClientError, match=message):
            async with McpCanaryClient(str(server.make_url("/mcp"))):
                pass
    finally:
        await server.close()
