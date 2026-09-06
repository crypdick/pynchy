"""Expose one trusted stdio MCP server on loopback Streamable HTTP.

The Pynchy MCP proxy remains the only route agents can use, so its security
gate still approves every tool call before it reaches this bridge.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
)
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

if TYPE_CHECKING:
    from mcp.types import CallToolResult, Tool

    type _ListToolsHandler = Callable[[], Awaitable[list[Tool]]]
    type _CallToolHandler = Callable[[str, dict[str, Any]], Awaitable[CallToolResult]]
    type _ListToolsDecorator = Callable[[_ListToolsHandler], _ListToolsHandler]
    type _CallToolDecorator = Callable[[_CallToolHandler], _CallToolHandler]

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


def _health(_: object) -> Response:
    return Response(status_code=204)


def _application(command: list[str]) -> Starlette:
    server = Server("pynchy-stdio-bridge")
    backend_session: ClientSession | None = None

    # mcp 1.26 leaves its callback decorators untyped. Their runtime contract
    # preserves the registered callback, so isolate that missing SDK typing here.
    list_tools_decorator = cast("Callable[[], _ListToolsDecorator]", server.list_tools)
    call_tool_decorator = cast("Callable[[], _CallToolDecorator]", server.call_tool)

    def connected_session() -> ClientSession:
        if backend_session is None:
            raise RuntimeError("Stdio MCP backend is not connected")
        return backend_session

    @list_tools_decorator()
    async def list_tools() -> list[Tool]:
        return (await connected_session().list_tools()).tools

    @call_tool_decorator()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        return await connected_session().call_tool(name, arguments)

    session_manager = StreamableHTTPSessionManager(server, json_response=True)
    backend_params = StdioServerParameters(
        command=command[0],
        args=command[1:],
        env=dict(os.environ),
    )

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
