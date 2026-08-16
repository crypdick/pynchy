"""One end-to-end check for the managed stdio MCP bridge."""

from __future__ import annotations

import asyncio
import socket
import subprocess  # noqa: S404 - test starts a local stdio MCP fixture.
import sys
from contextlib import asynccontextmanager
from unittest.mock import ANY, Mock

import aiohttp
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from pynchy.host.container_manager.mcp import stdio_bridge

_BACKEND = """
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("test-stdio-backend")

@server.list_tools()
async def list_tools():
    return [Tool(name="echo", description="echo", inputSchema={"type": "object"})]

@server.call_tool()
async def call_tool(name, arguments):
    return [TextContent(type="text", text=arguments["value"])]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

asyncio.run(main())
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_bridge(port: int, backend: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - fixed test command, no shell.
        [
            sys.executable,
            "-m",
            "pynchy.host.container_manager.mcp.stdio_bridge",
            "--port",
            str(port),
            "--",
            sys.executable,
            backend,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _wait_for_bridge(url: str, process: subprocess.Popen[bytes]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("stdio bridge exited before it started")
        try:
            async with aiohttp.ClientSession() as session, session.get(url) as response:
                if response.status < 500:
                    return
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.05)
    raise TimeoutError("stdio bridge did not become reachable")


@pytest.mark.asyncio
async def test_stdio_bridge_proxies_tool_discovery_and_calls(tmp_path):
    backend = tmp_path / "backend.py"
    backend.write_text(_BACKEND)
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    process = await asyncio.to_thread(_start_bridge, port, str(backend))
    try:
        await _wait_for_bridge(url, process)
        async with (
            aiohttp.ClientSession() as client,
            client.get(f"http://127.0.0.1:{port}/") as response,
        ):
            assert response.status == 204
        async with (
            streamable_http_client(url) as (read_stream, write_stream, _),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("echo", {"value": "healthy"})

        assert [tool.name for tool in tools.tools] == ["echo"]
        assert result.content[0].text == "healthy"
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)


def test_stdio_bridge_cli_starts_a_loopback_server_for_its_backend_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Mock()
    monkeypatch.setattr(stdio_bridge.uvicorn, "run", started)
    monkeypatch.setattr(
        sys,
        "argv",
        ["stdio-bridge", "--port", "8765", "--", "backend", "--safe-mode"],
    )

    stdio_bridge.main()

    started.assert_called_once_with(
        ANY,
        host="127.0.0.1",
        port=8765,
        log_level="warning",
        access_log=False,
    )


def test_stdio_bridge_cli_requires_a_backend_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["stdio-bridge", "--port", "8765"])

    with pytest.raises(SystemExit):
        stdio_bridge.main()


@pytest.mark.asyncio
async def test_stdio_bridge_cli_lifespan_initializes_and_closes_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Mock()
    backend_parameters = []
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:10.42.0.1:5037")
    monkeypatch.setattr(stdio_bridge.uvicorn, "run", started)
    monkeypatch.setattr(
        sys,
        "argv",
        ["stdio-bridge", "--port", "8765", "--", "backend"],
    )

    @asynccontextmanager
    async def fake_stdio_client(parameters):
        backend_parameters.append(parameters)
        yield object(), object()

    class FakeSession:
        instances = []

        def __init__(self, *_args):
            self.initialized = False
            self.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

        async def initialize(self):
            self.initialized = True

    class FakeSessionManager:
        def __init__(self, _server, *, json_response):
            assert json_response is True

        async def handle_request(self, _scope, _receive, _send):
            return None

        @asynccontextmanager
        async def run(self):
            yield

    monkeypatch.setattr(stdio_bridge, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(stdio_bridge, "ClientSession", FakeSession)
    monkeypatch.setattr(stdio_bridge, "StreamableHTTPSessionManager", FakeSessionManager)

    stdio_bridge.main()
    app = started.call_args.args[0]

    async with app.router.lifespan_context(app):
        session = FakeSession.instances[-1]
        assert session.initialized

    assert backend_parameters[-1].env["ADB_SERVER_SOCKET"] == "tcp:10.42.0.1:5037"


@pytest.mark.asyncio
async def test_stdio_bridge_rejects_requests_before_backend_connection(monkeypatch) -> None:
    handlers = {}
    started = Mock()

    class FakeServer:
        def __init__(self, _name):
            pass

        def list_tools(self):
            return lambda handler: handlers.setdefault("list_tools", handler)

        def call_tool(self):
            return lambda handler: handlers.setdefault("call_tool", handler)

    class FakeSessionManager:
        def __init__(self, _server, *, json_response):
            assert json_response is True

        async def handle_request(self, _scope, _receive, _send):
            return None

        @asynccontextmanager
        async def run(self):
            yield

    monkeypatch.setattr(stdio_bridge, "Server", FakeServer)
    monkeypatch.setattr(stdio_bridge, "StreamableHTTPSessionManager", FakeSessionManager)
    monkeypatch.setattr(stdio_bridge.uvicorn, "run", started)
    monkeypatch.setattr(sys, "argv", ["stdio-bridge", "--port", "8765", "--", "backend"])
    stdio_bridge.main()

    with pytest.raises(RuntimeError, match="backend is not connected"):
        await handlers["list_tools"]()


@pytest.mark.asyncio
async def test_stdio_bridge_forwards_requests_after_backend_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers = {}
    started = Mock()

    class FakeServer:
        def __init__(self, _name):
            pass

        def list_tools(self):
            return lambda handler: handlers.setdefault("list_tools", handler)

        def call_tool(self):
            return lambda handler: handlers.setdefault("call_tool", handler)

    class FakeSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

        async def initialize(self):
            return None

        async def list_tools(self):
            return type("ToolResult", (), {"tools": ["echo"]})()

        async def call_tool(self, name, arguments):
            return {"name": name, "arguments": arguments}

    @asynccontextmanager
    async def fake_stdio_client(_parameters):
        yield object(), object()

    class FakeSessionManager:
        def __init__(self, _server, *, json_response):
            assert json_response is True

        async def handle_request(self, _scope, _receive, _send):
            return None

        @asynccontextmanager
        async def run(self):
            yield

    monkeypatch.setattr(stdio_bridge, "Server", FakeServer)
    monkeypatch.setattr(stdio_bridge, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(stdio_bridge, "ClientSession", FakeSession)
    monkeypatch.setattr(stdio_bridge, "StreamableHTTPSessionManager", FakeSessionManager)
    monkeypatch.setattr(stdio_bridge.uvicorn, "run", started)
    monkeypatch.setattr(sys, "argv", ["stdio-bridge", "--port", "8765", "--", "backend"])

    stdio_bridge.main()
    app = started.call_args.args[0]

    async with app.router.lifespan_context(app):
        assert await handlers["list_tools"]() == ["echo"]
        assert await handlers["call_tool"]("echo", {"value": "ok"}) == {
            "name": "echo",
            "arguments": {"value": "ok"},
        }
