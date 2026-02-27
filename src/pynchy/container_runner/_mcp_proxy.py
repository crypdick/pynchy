"""MCP proxy -- routes all MCP traffic through SecurityGate.

Lightweight aiohttp server managed by McpManager. Single port, path-based
routing: POST /mcp/<group_folder>/<invocation_ts>/<instance_id>

Applies:
- SecurityGate policy evaluation on every request
- Untrusted content fencing on responses from public_source=true servers
- Cop inspection on responses from public_source=true servers
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web

from pynchy.logger import logger
from pynchy.security.cop import inspect_inbound
from pynchy.security.fencing import fence_untrusted_content
from pynchy.security.gate import SecurityGate, get_gate


@dataclass
class _ProxyState:
    """Mutable routing state for the proxy.

    Stored as a single app-key value at construction time so that
    update_routes() can mutate the contents without touching the
    frozen app dict.
    """

    instance_urls: dict[str, str] = field(default_factory=dict)
    trust_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    http_session: aiohttp.ClientSession | None = None


# Typed app key -- set once at construction, never reassigned.
_STATE_KEY: web.AppKey[_ProxyState] = web.AppKey("proxy_state", t=_ProxyState)


def create_proxy_app(
    instance_urls: dict[str, str],
    *,
    trust_map: dict[str, dict[str, Any]] | None = None,
) -> web.Application:
    """Create the aiohttp proxy application.

    Args:
        instance_urls: Mapping of instance_id -> backend URL.
        trust_map: Mapping of instance_id -> trust properties dict.
            Used to decide whether to apply fencing (public_source=True).
    """
    app = web.Application()
    app[_STATE_KEY] = _ProxyState(
        instance_urls=instance_urls,
        trust_map=trust_map or {},
    )
    app.router.add_route(
        "*",
        "/mcp/{group_folder}/{invocation_ts}/{instance_id}{tail:.*}",
        _proxy_handler,
    )
    app.on_startup.append(_start_http_session)
    app.on_cleanup.append(_cleanup_http_session)
    return app


async def _start_http_session(app: web.Application) -> None:
    app[_STATE_KEY].http_session = aiohttp.ClientSession()


async def _cleanup_http_session(app: web.Application) -> None:
    session = app[_STATE_KEY].http_session
    if session:
        await session.close()
        app[_STATE_KEY].http_session = None


async def _proxy_handler(request: web.Request) -> web.Response:
    """Route an MCP request through SecurityGate to the backend."""
    group_folder = request.match_info["group_folder"]
    instance_id = request.match_info["instance_id"]
    tail = request.match_info.get("tail", "")

    try:
        invocation_ts = float(request.match_info["invocation_ts"])
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid invocation_ts"}, status=400)

    state = request.app[_STATE_KEY]

    # Look up backend URL
    backend_url = state.instance_urls.get(instance_id)
    if backend_url is None:
        return web.json_response(
            {"error": f"Unknown MCP instance: {instance_id}"}, status=404
        )

    # Look up SecurityGate
    gate = get_gate(group_folder, invocation_ts)
    if gate is None:
        logger.warning(
            "MCP proxy: no SecurityGate",
            group=group_folder,
            invocation_ts=invocation_ts,
        )
        return web.json_response(
            {"error": "No security context for this session"}, status=403
        )

    # Forward to backend
    body = await request.read()
    target_url = backend_url + tail

    # Filter out hop-by-hop headers that shouldn't be forwarded
    forwarded_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    # Use the shared session (created by on_startup hook).
    session = state.http_session
    assert session is not None, "Proxy ClientSession not initialized"

    try:
        async with session.request(
            request.method,
            target_url,
            data=body,
            headers=forwarded_headers,
        ) as backend_resp:
            response_body = await backend_resp.read()
            response_headers = {
                k: v
                for k, v in backend_resp.headers.items()
                if k.lower() not in ("content-length", "transfer-encoding")
            }

            # Apply fencing to responses from public_source servers
            trust = state.trust_map.get(instance_id, {})
            if trust.get("public_source"):
                response_body = await _apply_fencing(
                    response_body, instance_id, gate, group_folder
                )

            return web.Response(
                status=backend_resp.status,
                body=response_body,
                headers=response_headers,
            )
    except aiohttp.ClientError as exc:
        logger.error(
            "MCP proxy backend error", instance=instance_id, error=str(exc)
        )
        return web.json_response(
            {"error": "MCP backend unavailable"}, status=502
        )


async def _apply_fencing(
    response_body: bytes,
    instance_id: str,
    gate: SecurityGate,
    group_folder: str,
) -> bytes:
    """Apply untrusted content fencing and Cop inspection to MCP response.

    For each text content block in the MCP result:
    1. Record the read on the SecurityGate (sets corruption taint)
    2. Run Cop inspection for prompt injection detection
    3. If Cop flags the content, replace it with a warning
    4. Otherwise, wrap with fence markers via fence_untrusted_content
    """
    try:
        data = _json.loads(response_body)
    except (ValueError, UnicodeDecodeError):
        return response_body

    # Record read from public source (sets corruption taint)
    gate.evaluate_read(instance_id)

    # Fence text content in MCP result
    result = data.get("result", {})
    contents = result.get("content", [])
    for item in contents:
        if item.get("type") == "text" and "text" in item:
            verdict = await inspect_inbound(
                source=f"mcp:{instance_id}",
                content=item["text"],
            )
            if verdict.flagged:
                logger.warning(
                    "Cop flagged MCP response",
                    instance=instance_id,
                    group=group_folder,
                    reason=verdict.reason,
                )
                item["text"] = (
                    "Browser content blocked by security policy. "
                    "The page may contain unsafe content. Try a different page."
                )
            else:
                item["text"] = fence_untrusted_content(
                    item["text"], source=f"mcp:{instance_id}"
                )

    return _json.dumps(data).encode()


class McpProxy:
    """Manages the aiohttp proxy server lifecycle.

    Designed to be owned by McpManager. Starts on a dynamic port and
    provides URL-based routing so containers can reach their MCP backends
    through a single endpoint.
    """

    def __init__(self) -> None:
        self._runner: web.AppRunner | None = None
        self._port: int = 0

    @property
    def port(self) -> int:
        return self._port

    async def start(
        self,
        instance_urls: dict[str, str],
        *,
        trust_map: dict[str, dict[str, Any]] | None = None,
        port: int = 0,
    ) -> int:
        """Start the proxy server. Returns the assigned port.

        Args:
            instance_urls: Mapping of instance_id -> backend URL.
            trust_map: Mapping of instance_id -> trust properties.
            port: Port to bind to. 0 = OS-assigned dynamic port.
        """
        app = create_proxy_app(instance_urls, trust_map=trust_map)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "localhost", port)
        await site.start()
        # Extract the actual bound port from the socket
        self._port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        logger.info("MCP proxy started", port=self._port)
        return self._port

    async def stop(self) -> None:
        """Stop the proxy server and clean up resources."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("MCP proxy stopped")

    def update_routes(
        self,
        instance_urls: dict[str, str],
        trust_map: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Update the instance URL and trust mappings on a running proxy.

        Mutates the _ProxyState dataclass in-place rather than the
        frozen app dict -- safe to call while the server is running.
        """
        if self._runner and self._runner.app:
            state = self._runner.app[_STATE_KEY]
            state.instance_urls = instance_urls
            state.trust_map = trust_map or {}
