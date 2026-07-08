"""MCP proxy -- routes all MCP traffic through SecurityGate.

Lightweight aiohttp server managed by McpManager. Single port, path-based
routing: POST /mcp/<group_folder>/<invocation_ts>/<instance_id>

Applies:
- Outbound gating: evaluate_write() on tools/call before forwarding
  (forbidden → 403, needs_human → block until human approves/denies)
- Inbound fencing: untrusted content fencing on responses from public_source servers
- Cop inspection on responses from public_source=true servers
"""

from __future__ import annotations

import asyncio
import json as _json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web

from pynchy.host.container_manager.security.approval import (
    APPROVAL_TIMEOUT_SECONDS,
    register_mcp_proxy_approval,
)
from pynchy.host.container_manager.security.cop import inspect_inbound
from pynchy.host.container_manager.security.fencing import fence_untrusted_content
from pynchy.host.container_manager.security.gate import SecurityGate, get_gate
from pynchy.logger import logger

# Callback to request human approval.  Provided by the orchestrator at
# construction time.  Signature: (group_folder, tool_name, request_data,
# request_id) -> None.  The implementation writes the pending file and
# broadcasts the notification to chat channels.
ApprovalRequestFn = Callable[[str, str, dict[str, Any], str], Awaitable[None]]


@dataclass
class _ProxyState:
    """Mutable routing state for the proxy.

    Stored as a single app-key value so its contents (e.g. ``http_session``,
    set once the aiohttp session is created) can be populated without
    replacing entries in the frozen app dict.
    """

    instance_urls: dict[str, str] = field(default_factory=dict)
    trust_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    http_session: aiohttp.ClientSession | None = None
    approval_fn: ApprovalRequestFn | None = None


# Typed app key -- set once at construction, never reassigned.
_STATE_KEY: web.AppKey[_ProxyState] = web.AppKey("proxy_state", t=_ProxyState)


@dataclass(frozen=True)
class _ProxyRequest:
    group_folder: str
    instance_id: str
    invocation_ts: float
    tail: str


def create_proxy_app(
    instance_urls: dict[str, str],
    *,
    trust_map: dict[str, dict[str, Any]] | None = None,
    approval_fn: ApprovalRequestFn | None = None,
) -> web.Application:
    """Create the aiohttp proxy application.

    Args:
        instance_urls: Mapping of instance_id -> backend URL.
        trust_map: Mapping of instance_id -> trust properties dict.
            Determines whether to apply fencing (public_source=True).
        approval_fn: Callback for human approval requests.  When a tools/call
            triggers needs_human, the proxy calls this to write the pending
            file and broadcast to chat, then blocks until the human responds.
    """
    app = web.Application()
    app[_STATE_KEY] = _ProxyState(
        instance_urls=instance_urls,
        trust_map=trust_map or {},
        approval_fn=approval_fn,
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


def _proxy_request(request: web.Request) -> _ProxyRequest | web.Response:
    try:
        invocation_ts = float(request.match_info["invocation_ts"])
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid invocation_ts"}, status=400)

    return _ProxyRequest(
        group_folder=request.match_info["group_folder"],
        instance_id=request.match_info["instance_id"],
        invocation_ts=invocation_ts,
        tail=request.match_info.get("tail", ""),
    )


def _backend_url(state: _ProxyState, proxy_request: _ProxyRequest) -> str | web.Response:
    backend_url = state.instance_urls.get(proxy_request.instance_id)
    if backend_url is not None:
        return backend_url
    return web.json_response(
        {"error": f"Unknown MCP instance: {proxy_request.instance_id}"},
        status=404,
    )


def _session_gate(proxy_request: _ProxyRequest) -> SecurityGate | web.Response:
    gate = get_gate(proxy_request.group_folder, proxy_request.invocation_ts)
    if gate is not None:
        return gate

    logger.warning(
        "MCP proxy: no SecurityGate",
        group=proxy_request.group_folder,
        invocation_ts=proxy_request.invocation_ts,
    )
    return web.json_response({"error": "No security context for this session"}, status=403)


async def _rpc_payload(body: bytes) -> dict[str, Any]:
    try:
        return _json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        return {}


async def _maybe_gate_outbound_call(
    state: _ProxyState,
    proxy_request: _ProxyRequest,
    gate: SecurityGate,
    rpc: dict[str, Any],
) -> web.Response | None:
    if rpc.get("method") != "tools/call":
        return None

    capability = _mcp_capability_id(proxy_request.instance_id, rpc)
    capability_decision = gate.evaluate_capability(capability)
    if not capability_decision.allowed:
        return web.json_response(
            {"error": f"Policy denied: {capability_decision.reason}"},
            status=403,
        )
    if capability_decision.needs_human:
        approval_response = await _await_human_approval(
            state,
            proxy_request.group_folder,
            proxy_request.instance_id,
            rpc,
            capability_decision.reason or "",
        )
        if approval_response is not None:
            return approval_response

    decision = gate.evaluate_write(proxy_request.instance_id, rpc.get("params", {}))
    if not decision.allowed:
        return web.json_response({"error": f"Policy denied: {decision.reason}"}, status=403)
    if not decision.needs_human:
        return None

    return await _await_human_approval(
        state,
        proxy_request.group_folder,
        proxy_request.instance_id,
        rpc,
        decision.reason or "",
    )


def _mcp_capability_id(instance_id: str, rpc: dict[str, Any]) -> str:
    tool_name = rpc.get("params", {}).get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return f"mcp.{instance_id}"
    return f"mcp.{instance_id}.{tool_name.strip()}"


def _forwarded_headers(request: web.Request) -> dict[str, str]:
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in ("host", "content-length")
    }


async def _forward_to_backend(
    *,
    session: aiohttp.ClientSession,
    request: web.Request,
    backend_url: str,
    tail: str,
    body: bytes,
    state: _ProxyState,
    instance_id: str,
    gate: SecurityGate,
    group_folder: str,
) -> web.Response:
    try:
        async with session.request(
            request.method,
            backend_url + tail,
            data=body,
            headers=_forwarded_headers(request),
        ) as backend_resp:
            response_body = await backend_resp.read()
            response_headers = {
                key: value
                for key, value in backend_resp.headers.items()
                if key.lower() not in ("content-length", "transfer-encoding")
            }

            trust = state.trust_map.get(instance_id, {})
            if trust.get("public_source"):
                response_body = await _apply_fencing(response_body, instance_id, gate, group_folder)

            return web.Response(
                status=backend_resp.status,
                body=response_body,
                headers=response_headers,
            )
    except aiohttp.ClientError as exc:
        logger.error("MCP proxy backend error", instance=instance_id, error=str(exc))
        return web.json_response({"error": "MCP backend unavailable"}, status=502)


async def _proxy_handler(request: web.Request) -> web.Response:
    """Route an MCP request through SecurityGate to the backend."""
    proxy_request = _proxy_request(request)
    if isinstance(proxy_request, web.Response):
        return proxy_request

    state = request.app[_STATE_KEY]
    backend_url = _backend_url(state, proxy_request)
    if isinstance(backend_url, web.Response):
        return backend_url
    gate = _session_gate(proxy_request)
    if isinstance(gate, web.Response):
        return gate

    body = await request.read()
    rpc = await _rpc_payload(body)
    outbound_decision = await _maybe_gate_outbound_call(state, proxy_request, gate, rpc)
    if outbound_decision is not None:
        return outbound_decision

    session = state.http_session
    assert session is not None, "Proxy ClientSession not initialized"
    return await _forward_to_backend(
        session=session,
        request=request,
        backend_url=backend_url,
        tail=proxy_request.tail,
        body=body,
        state=state,
        instance_id=proxy_request.instance_id,
        gate=gate,
        group_folder=proxy_request.group_folder,
    )


async def _await_human_approval(
    state: _ProxyState,
    group_folder: str,
    instance_id: str,
    rpc: dict[str, Any],
    reason: str,
) -> web.Response | None:
    """Block the HTTP connection until the human approves or denies.

    Returns a web.Response to send back to the client if denied/timed out,
    or None if approved (caller should proceed to forward the request).
    """
    if state.approval_fn is None:
        return web.json_response(
            {
                "error": (
                    "This action requires human approval but no approval "
                    "handler is configured. Ask the user to perform this "
                    f"action directly. Reason: {reason}"
                ),
            },
            status=403,
        )

    request_id = str(uuid.uuid4())
    fut = register_mcp_proxy_approval(request_id)

    tool_name = rpc.get("params", {}).get("name", instance_id)
    await state.approval_fn(group_folder, tool_name, rpc, request_id)

    logger.info(
        "MCP proxy awaiting human approval",
        tool_name=tool_name,
        group=group_folder,
        request_id=request_id[:8],
        reason=reason,
    )

    try:
        approved = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "MCP proxy approval timed out",
            request_id=request_id[:8],
            group=group_folder,
        )
        return web.json_response(
            {"error": "Human approval timed out"},
            status=408,
        )

    if not approved:
        return web.json_response(
            {"error": "Action denied by human"},
            status=403,
        )

    # Approved — return None to let the caller forward the request
    logger.info(
        "MCP proxy approval granted",
        request_id=request_id[:8],
        group=group_folder,
    )
    return None


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
    3. If Cop flags the content, substitute a warning
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
                item["text"] = fence_untrusted_content(item["text"], source=f"mcp:{instance_id}")

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
        approval_fn: ApprovalRequestFn | None = None,
        port: int = 0,
    ) -> int:
        """Start the proxy server. Returns the assigned port.

        Args:
            instance_urls: Mapping of instance_id -> backend URL.
            trust_map: Mapping of instance_id -> trust properties.
            approval_fn: Callback for human approval requests.
            port: Port to bind to. 0 = OS-assigned dynamic port.
        """
        app = create_proxy_app(instance_urls, trust_map=trust_map, approval_fn=approval_fn)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "localhost", port)
        await site.start()
        # Extract the actual bound port from the socket
        self._port = site._server.sockets[0].getsockname()[1]
        logger.info("MCP proxy started", port=self._port)
        return self._port

    async def stop(self) -> None:
        """Stop the proxy server and clean up resources."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("MCP proxy stopped")
