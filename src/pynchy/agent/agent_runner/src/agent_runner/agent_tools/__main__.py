"""Allow running as: python -m agent_runner.agent_tools"""

import asyncio

from ._server import run_server

asyncio.run(run_server())
