"""Expose one trusted stdio MCP server on loopback Streamable HTTP.

The Pynchy MCP proxy remains the only route agents can use, so its security
gate still approves every tool call before it reaches this bridge.
"""

from __future__ import annotations

import argparse

from fastmcp.client.transports import StdioTransport
from fastmcp.server import create_proxy

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


def main() -> None:
    """Run the bridge until its Pynchy-managed process group terminates it."""
    port, command = _arguments()
    server = create_proxy(
        StdioTransport(command[0], command[1:], keep_alive=True),
        name="pynchy-stdio-bridge",
    )
    server.run(
        transport="streamable-http",
        host=_LOOPBACK_HOST,
        port=port,
        path="/mcp",
        show_banner=False,
        log_level="warning",
        uvicorn_config={"access_log": False},
    )


if __name__ == "__main__":
    main()
