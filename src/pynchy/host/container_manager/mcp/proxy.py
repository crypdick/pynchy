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
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, cast

import aiohttp
from aiohttp import web

from pynchy.content_fencing import fence_untrusted_content
from pynchy.host.container_manager.mcp.sse import stream_response
from pynchy.host.container_manager.security.approval import register_mcp_proxy_approval
from pynchy.host.container_manager.security.cop import inspect_inbound
from pynchy.host.container_manager.security.gate import SecurityGate, get_gate
from pynchy.logger import logger
from pynchy.workspace.api import APPROVAL_TIMEOUT_SECONDS

# Callback to request human approval.  Provided by the orchestrator at
# construction time.  Signature: (group_folder, tool_name, request_data,
# request_id) -> None.  The implementation writes the pending file and
# broadcasts the notification to chat channels.
ApprovalRequestFn = Callable[[str, str, dict[str, Any], str], Awaitable[None]]
BackendLeaseFn = Callable[[str], AbstractAsyncContextManager[None]]
AuthorizeInstanceFn = Callable[[str, str], bool]


class McpBackendUnavailableError(RuntimeError):
    """A managed MCP backend could not be leased for a proxied request."""


@dataclass
class _ProxyState:
    """Mutable routing state for the proxy.

    Stored as a single app-key value so its contents (e.g. ``http_session``,
    set once the aiohttp session is created) can be populated without
    replacing entries in the frozen app dict.
    """

    instance_urls: dict[str, str] = field(default_factory=dict)
    trust_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    service_names: dict[str, str] = field(default_factory=dict)
    http_session: aiohttp.ClientSession | None = None
    approval_fn: ApprovalRequestFn | None = None
    backend_lease: BackendLeaseFn | None = None
    authorize_instance: AuthorizeInstanceFn | None = None


# Typed app key -- set once at construction, never reassigned.
_STATE_KEY: web.AppKey[_ProxyState] = web.AppKey("proxy_state", t=_ProxyState)

_MCP_PROXY_NO_BOUND_ADDRESSES = "MCP proxy server did not bind any addresses"
_MCP_PROXY_HTTP_SESSION_UNINITIALIZED = "MCP proxy ClientSession not initialized"


@dataclass(frozen=True)
class _ProxyRequest:
    group_folder: str
    instance_id: str
    invocation_ts: float
    tail: str


@dataclass(frozen=True)
class _BackendForwardContext:
    session: aiohttp.ClientSession
    request: object
    backend_url: str
    tail: str
    body: bytes
    state: _ProxyState
    instance_id: str
    gate: SecurityGate
    group_folder: str


def create_proxy_app(  # noqa: PLR0913 - proxy composition keeps security callbacks explicit.
    instance_urls: dict[str, str],
    *,
    trust_map: dict[str, dict[str, Any]] | None = None,
    service_names: dict[str, str] | None = None,
    approval_fn: ApprovalRequestFn | None = None,
    backend_lease: BackendLeaseFn | None = None,
    authorize_instance: AuthorizeInstanceFn | None = None,
) -> object:
    """Create the aiohttp proxy application.

    Args:
        instance_urls: Mapping of instance_id -> backend URL.
        trust_map: Mapping of instance_id -> trust properties dict.
            Determines whether to apply fencing (public_source=True).
        service_names: Mapping of instance_id -> configured service name for
            policy and capability evaluation.
        approval_fn: Callback for human approval requests.  When a tools/call
            triggers needs_human, the proxy calls this to write the pending
            file and broadcast to chat, then blocks until the human responds.
        backend_lease: Context manager that keeps a managed backend available
            while a valid request is forwarded.
        authorize_instance: Optional workspace-to-instance authorization check.
    """
    app = web.Application()
    app[_STATE_KEY] = _ProxyState(
        instance_urls=instance_urls,
        trust_map=trust_map or {},
        service_names=service_names or {},
        approval_fn=approval_fn,
        backend_lease=backend_lease,
        authorize_instance=authorize_instance,
    )
    app.router.add_route(
        "*",
        "/mcp/{group_folder}/{invocation_ts}/{instance_id}{tail:.*}",
        _proxy_handler,
    )
    app.on_startup.append(_start_http_session)
    app.on_cleanup.append(_cleanup_http_session)
    return app


async def _start_http_session(app: object) -> None:  # noqa: RUF029 - aiohttp startup callbacks are async.
    cast("web.Application", app)[_STATE_KEY].http_session = aiohttp.ClientSession()


async def _cleanup_http_session(app: object) -> None:
    web_app = cast("web.Application", app)
    session = web_app[_STATE_KEY].http_session
    if session:
        await session.close()
        web_app[_STATE_KEY].http_session = None


def _runner_port(runner: web.AppRunner) -> int:
    addresses = runner.addresses
    if not addresses:
        raise RuntimeError(_MCP_PROXY_NO_BOUND_ADDRESSES)

    address = addresses[0]
    return int(address[1])


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
    if backend_url is not None and (
        state.authorize_instance is None
        or state.authorize_instance(proxy_request.group_folder, proxy_request.instance_id)
    ):
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


def _rpc_payload(body: bytes) -> dict[str, Any]:
    try:
        return _json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        return {}


async def _maybe_gate_outbound_call(
    state: _ProxyState,
    proxy_request: _ProxyRequest,
    gate: SecurityGate,
    rpc: dict[str, Any],
    service_name: str,
) -> web.Response | None:
    if rpc.get("method") != "tools/call":
        return None

    capability = _mcp_capability_id(service_name, rpc)
    capability_decision = gate.evaluate_capability(capability)
    if not capability_decision.allowed:
        return web.json_response(
            {"error": f"Policy denied: {capability_decision.reason}"},
            status=403,
        )
    decision = gate.evaluate_write(service_name, rpc.get("params", {}))
    if not decision.allowed:
        return web.json_response({"error": f"Policy denied: {decision.reason}"}, status=403)
    needs_human = capability_decision.needs_human or (
        decision.needs_human and not capability_decision.overrides_human_approval
    )
    if not needs_human:
        return None

    return await _await_human_approval(
        state,
        proxy_request.group_folder,
        proxy_request.instance_id,
        rpc,
        "; ".join(reason for reason in (capability_decision.reason, decision.reason) if reason),
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


def _response_headers(response: aiohttp.ClientResponse) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in ("content-length", "transfer-encoding")
    }


async def _transform_sse_data(payload: bytes, context: _BackendForwardContext) -> bytes:
    trust = context.state.trust_map.get(context.instance_id, {})
    payload = _filter_denied_tools_list(payload, context)
    if not trust.get("public_source"):
        return payload
    return await _apply_fencing(
        payload,
        context.instance_id,
        context.gate,
        context.group_folder,
    )


async def _backend_response(
    context: _BackendForwardContext,
) -> web.StreamResponse:
    request = cast("web.Request", context.request)
    # Codex and other streamable-HTTP clients address the proxy at ``.../mcp``.
    # The managed backend endpoint already includes that suffix, so forwarding it
    # a second time would turn a valid endpoint into ``.../mcp/mcp``.
    backend_url = context.backend_url
    if context.tail and not backend_url.rstrip("/").endswith(context.tail):
        backend_url = f"{backend_url}{context.tail}"
    async with context.session.request(
        request.method,
        backend_url,
        data=context.body,
        headers=_forwarded_headers(request),
    ) as backend_resp:
        if backend_resp.content_type == "text/event-stream":
            return await stream_response(
                request,
                backend_resp,
                lambda payload: _transform_sse_data(payload, context),
            )

        response_body = await backend_resp.read()

        response_body = _filter_denied_tools_list(response_body, context)

        trust = context.state.trust_map.get(context.instance_id, {})
        if trust.get("public_source"):
            response_body = await _apply_fencing(
                response_body,
                context.instance_id,
                context.gate,
                context.group_folder,
            )

        return web.Response(
            status=backend_resp.status,
            body=response_body,
            headers=_response_headers(backend_resp),
        )


def _filter_denied_tools_list(response_body: bytes, context: _BackendForwardContext) -> bytes:
    if _rpc_payload(context.body).get("method") != "tools/list":
        return response_body
    payload = _filtered_tools_payload(_rpc_payload(response_body), context)
    if payload is not None:
        return _json.dumps(payload).encode()
    return response_body


def _filtered_tools_payload(
    payload: dict[str, Any], context: _BackendForwardContext
) -> dict[str, Any] | None:
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return None
    service_name = context.state.service_names.get(context.instance_id, context.instance_id)
    result["tools"] = [
        tool
        for tool in result["tools"]
        if not isinstance(tool, dict)
        or not isinstance(tool.get("name"), str)
        or context.gate.evaluate_capability(
            _mcp_capability_id(service_name, {"params": {"name": tool["name"]}})
        ).allowed
    ]
    return payload


async def _forward_to_backend(
    context: _BackendForwardContext,
) -> web.StreamResponse:
    try:
        return await _backend_response(context)
    except aiohttp.ClientError as exc:
        logger.error("MCP proxy backend error", instance=context.instance_id, error=str(exc))
        return web.json_response({"error": "MCP backend unavailable"}, status=502)


async def _forward_with_backend_lease(
    context: _BackendForwardContext,
) -> web.StreamResponse:
    backend_lease = context.state.backend_lease
    if backend_lease is None:
        return await _forward_to_backend(context)
    try:
        async with backend_lease(context.instance_id):
            return await _forward_to_backend(context)
    except McpBackendUnavailableError:
        return web.json_response({"error": "MCP backend unavailable"}, status=502)


async def _proxy_handler(request: web.Request) -> web.StreamResponse:
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
    rpc = _rpc_payload(body)
    service_name = state.service_names.get(proxy_request.instance_id, proxy_request.instance_id)
    outbound_decision = await _maybe_gate_outbound_call(
        state,
        proxy_request,
        gate,
        rpc,
        service_name,
    )
    if outbound_decision is not None:
        return outbound_decision

    session = state.http_session
    if session is None:
        raise RuntimeError(_MCP_PROXY_HTTP_SESSION_UNINITIALIZED)
    return await _forward_with_backend_lease(
        _BackendForwardContext(
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

    raw_tool_name = rpc.get("params", {}).get("name")
    tool_name = raw_tool_name if isinstance(raw_tool_name, str) else instance_id
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
            if gate.policy.cop_active:
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
                    continue
            item["text"] = fence_untrusted_content(item["text"], source=f"mcp:{instance_id}")

    return _json.dumps(data).encode()


class McpProxy:
    """Manages the aiohttp proxy server lifecycle.

    Designed to be owned by McpManager. Starts on a dynamic port and
    provides URL-based routing so containers can reach their MCP backends
    through a single endpoint.
    """

    def __init__(
        self,
        *,
        host: str = "localhost",
        backend_lease: BackendLeaseFn | None = None,
        authorize_instance: AuthorizeInstanceFn | None = None,
    ) -> None:
        self._host = host
        self._backend_lease = backend_lease
        self._authorize_instance = authorize_instance
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
        service_names: dict[str, str] | None = None,
        approval_fn: ApprovalRequestFn | None = None,
        port: int = 0,
    ) -> int:
        """Start the proxy server. Returns the assigned port.

        Args:
            instance_urls: Mapping of instance_id -> backend URL.
            trust_map: Mapping of instance_id -> trust properties.
            service_names: Mapping of instance_id -> configured service name.
            approval_fn: Callback for human approval requests.
            port: Port to bind to. 0 = OS-assigned dynamic port.
        """
        app = cast(
            "web.Application",
            create_proxy_app(
                instance_urls,
                trust_map=trust_map,
                service_names=service_names,
                approval_fn=approval_fn,
                backend_lease=self._backend_lease,
                authorize_instance=self._authorize_instance,
            ),
        )
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, port)
        await site.start()
        # Extract the actual bound port from the public runner address list.
        self._port = _runner_port(self._runner)
        logger.info("MCP proxy started", host=self._host, port=self._port)
        return self._port

    async def stop(self) -> None:
        """Stop the proxy server and clean up resources."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("MCP proxy stopped")
