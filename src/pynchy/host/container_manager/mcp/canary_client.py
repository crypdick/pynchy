"""Small Streamable HTTP MCP client for provider-boundary canaries.

The agent cores own their full MCP clients.  Operational canaries only need a
deliberately narrow subset of the protocol: initialize a session, inspect the
published tools, and invoke one tool.  Keeping this client here means a
canary exercises the same managed container and OAuth material an agent uses,
without asking an LLM to interpret provider output.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp

_MCP_PROTOCOL_VERSION = "2025-06-18"
_MCP_ACCEPT = "application/json, text/event-stream"


class McpCanaryClientError(RuntimeError):
    """The managed MCP server rejected or malformed a canary request."""


class McpCanaryToolError(McpCanaryClientError):
    """A tool call completed with an MCP error result."""


class McpCanaryClient:
    """Minimal stateful client for a Streamable HTTP MCP endpoint."""

    def __init__(self, endpoint_url: str) -> None:
        self._endpoint_url = endpoint_url
        self._session: aiohttp.ClientSession | None = None
        self._mcp_session_id: str | None = None
        self._request_id = 0

    async def __aenter__(self) -> McpCanaryClient:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            await self._initialize()
        except Exception:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def list_tool_names(self) -> set[str]:
        """Return the tool names the server actually published for this session."""
        result = await self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpCanaryClientError("MCP server returned no tool list")
        names = {
            tool["name"]
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"]
        }
        if not names:
            raise McpCanaryClientError("MCP server published no callable tools")
        return names

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, Any]:
        """Invoke one MCP tool and reject provider-declared error results."""
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        if result.get("isError") is True:
            raise McpCanaryToolError("MCP tool reported an error")
        return result

    async def _initialize(self) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pynchy-operational-canary", "version": "1.0"},
            },
        )
        await self._notify("notifications/initialized")

    async def _notify(self, method: str) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": {}})

    async def _request(self, method: str, params: dict[str, object]) -> dict[str, Any]:
        self._request_id += 1
        payload = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
        )
        if payload is None:
            raise McpCanaryClientError("MCP server returned no response")
        if "error" in payload:
            raise McpCanaryToolError("MCP server rejected the request")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpCanaryClientError("MCP server returned a non-object result")
        return result

    async def _post(self, body: dict[str, object]) -> dict[str, Any] | None:
        if self._session is None:
            raise McpCanaryClientError("MCP client was used outside its context")
        headers = {"Accept": _MCP_ACCEPT, "Content-Type": "application/json"}
        if self._mcp_session_id is not None:
            headers["Mcp-Session-Id"] = self._mcp_session_id
        try:
            response = await self._session.post(
                self._endpoint_url,
                json=body,
                headers=headers,
            )
        except aiohttp.ClientError as exc:
            raise McpCanaryClientError("MCP server is unavailable") from exc
        async with response:
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self._mcp_session_id = session_id
            text = await response.text()
            if response.status >= 400:
                raise McpCanaryToolError("MCP server returned an HTTP error")
        if not text.strip():
            return None
        return _response_payload(text)


def _response_payload(body: str) -> dict[str, Any]:
    """Decode a JSON-RPC object from JSON or a one-response SSE body."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = _sse_payload(body)
    if not isinstance(payload, dict):
        raise McpCanaryClientError("MCP server returned a non-object response")
    return payload


def _sse_payload(body: str) -> object:
    data_lines = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
    if not data_lines:
        raise McpCanaryClientError("MCP server returned an invalid response")
    try:
        return json.loads("\n".join(data_lines))
    except json.JSONDecodeError as exc:
        raise McpCanaryClientError("MCP server returned invalid SSE JSON") from exc
