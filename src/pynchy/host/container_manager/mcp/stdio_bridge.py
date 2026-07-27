"""Expose one trusted stdio MCP server on loopback Streamable HTTP.

The Pynchy MCP proxy remains the only route agents can use, so its security
gate still approves every tool call before it reaches this bridge.
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp.types import CallToolResult, Tool
    from starlette.requests import Request

_LOOPBACK_HOST = "127.0.0.1"


def _arguments() -> tuple[int, list[str]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args()
    command = parsed.command[1:] if parsed.command[:1] == ["--"] else parsed.command
    if not command:
        parser.error("a stdio MCP command is required after --")
    return parsed.port, command


def _health(_: Request) -> Response:
    return Response(status=204)


def _application(command: list[str]) -> Starlette:
    server = Server("pynchy-stdio-bridge")
    backend_session: ClientSession | None = None

    def connected_session() -> ClientSession:
        if backend_session is None:
            raise RuntimeError("Stdio MCP backend is not connected")
        return backend_session

    # mcp's Server decorators have no typed wrapper, but these functions
    # register the typed callbacks consumed only by the SDK.
    @server.list_tools()  # type: ignore[untyped-decorator]
    async def list_tools() -> list[Tool]:
        return (await connected_session().list_tools()).tools

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        return await connected_session().call_tool(name, arguments)

    session_manager = StreamableHTTPSessionManager(server, json_response=True)
    backend_params = StdioServerParameters(command=command[0], args=command[1:])

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        nonlocal backend_session
        async with (
            stdio_client(backend_params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            backend_session = session
            async with session_manager.run():
                yield
            backend_session = None

    return Starlette(
        routes=[Route("/", _health), Mount("/mcp", app=session_manager.handle_request)],
        lifespan=lifespan,
    )


def main() -> None:
    """Run the bridge until its Pynchy-managed process group terminates it."""
    port, command = _arguments()
    uvicorn.run(
        _application(command),
        host=_LOOPBACK_HOST,
        port=port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
