"""Public HTTP contract tests for the builtin LLM gateway."""

import pytest
from aiohttp import ClientSession, web

from pynchy.host.container_manager.gateway import BuiltinGateway
from pynchy.host.container_manager.gateway_builtin import BuiltinGatewayCredentials


async def _start_http_server(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert server is not None
    port = server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_proxies_authenticated_anthropic_request_to_provider(
    monkeypatch: pytest.MonkeyPatch, unused_tcp_port: int
) -> None:
    received: dict[str, str | bytes] = {}

    async def upstream(request: web.Request) -> web.Response:
        received["api_key"] = request.headers["x-api-key"]
        received["authorization"] = request.headers.get("Authorization", "")
        received["body"] = await request.read()
        return web.Response(status=201, body=b"proxied", headers={"X-Upstream": "ok"})

    app = web.Application()
    app.router.add_post("/v1/messages", upstream)
    upstream_runner, upstream_url = await _start_http_server(app)
    monkeypatch.setattr(
        "pynchy.host.container_manager.gateway_builtin._ANTHROPIC_BASE", upstream_url
    )
    gateway = BuiltinGateway(
        port=unused_tcp_port,
        host="127.0.0.1",
        container_host="gateway.test",
        credentials=BuiltinGatewayCredentials(
            anthropic_api_key="anthropic-secret"  # pragma: allowlist secret
        ),
    )
    body = b'{"model":"claude","messages":[]}'

    try:
        await gateway.start()
        async with (
            ClientSession() as client,
            client.post(
                f"http://127.0.0.1:{unused_tcp_port}/v1/messages",
                headers={"Authorization": f"Bearer {gateway.key}"},
                data=body,
            ) as response,
        ):
            assert response.status == 201
            assert response.headers["X-Upstream"] == "ok"
            assert await response.read() == b"proxied"
    finally:
        await gateway.stop()
        await upstream_runner.cleanup()

    assert received == {
        "api_key": "anthropic-secret",  # pragma: allowlist secret
        "authorization": "",
        "body": body,
    }


@pytest.mark.asyncio
async def test_rejects_unauthorized_unknown_and_unconfigured_requests(unused_tcp_port: int) -> None:
    gateway = BuiltinGateway(
        port=unused_tcp_port,
        host="127.0.0.1",
        container_host="gateway.test",
        credentials=BuiltinGatewayCredentials(
            anthropic_api_key="anthropic-secret"  # pragma: allowlist secret
        ),
    )

    try:
        await gateway.start()
        async with ClientSession() as client:
            response = await client.get(f"http://127.0.0.1:{unused_tcp_port}/v1/messages")
            assert response.status == 401
            response.close()
            headers = {"X-Api-Key": gateway.key}
            response = await client.get(
                f"http://127.0.0.1:{unused_tcp_port}/unsupported", headers=headers
            )
            assert response.status == 404
            response.close()
            response = await client.get(
                f"http://127.0.0.1:{unused_tcp_port}/v1/chat/completions", headers=headers
            )
            assert response.status == 503
            response.close()
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_rejects_invalid_body_and_reports_upstream_transport_failure(
    monkeypatch: pytest.MonkeyPatch, unused_tcp_port: int
) -> None:
    monkeypatch.setattr(
        "pynchy.host.container_manager.gateway_builtin._ANTHROPIC_BASE",
        "http://127.0.0.1:1",
    )
    gateway = BuiltinGateway(
        port=unused_tcp_port,
        host="127.0.0.1",
        container_host="gateway.test",
        credentials=BuiltinGatewayCredentials(
            anthropic_api_key="anthropic-secret"  # pragma: allowlist secret
        ),
    )

    try:
        await gateway.start()
        async with ClientSession() as client:
            headers = {"X-Api-Key": gateway.key}
            response = await client.post(
                f"http://127.0.0.1:{unused_tcp_port}/v1/messages",
                headers=headers,
                data=b"not-json",
            )
            assert response.status == 400
            response.close()
            response = await client.post(
                f"http://127.0.0.1:{unused_tcp_port}/v1/messages",
                headers=headers,
                data=b'{"messages": []}',
            )
            assert response.status == 502
            response.close()
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_starts_without_credentials_and_reports_no_providers(unused_tcp_port: int) -> None:
    gateway = BuiltinGateway(
        port=unused_tcp_port,
        host="127.0.0.1",
        container_host="gateway.test",
    )

    await gateway.start()
    try:
        assert gateway.has_provider("anthropic") is False
        assert gateway.has_provider("openai") is False
    finally:
        await gateway.stop()
