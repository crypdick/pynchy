"""One end-to-end check for the managed stdio MCP bridge."""

from __future__ import annotations

import asyncio
import socket
import subprocess  # noqa: S404 - test starts a local stdio MCP fixture.
import sys
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
