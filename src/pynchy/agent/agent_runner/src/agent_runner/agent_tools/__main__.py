"""Run Pynchy's native stdio MCP server."""

import asyncio
import sys

from ._server import run_server


def main() -> int:
    if len(sys.argv) != 1:
        sys.stderr.write("usage: python -m agent_runner.agent_tools\n")
        return 2
    asyncio.run(run_server())
    return 0


raise SystemExit(main())
