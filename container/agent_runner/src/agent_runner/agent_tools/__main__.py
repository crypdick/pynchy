"""Allow running as: python -m agent_runner.agent_tools"""

import asyncio

from agent_runner.agent_tools._server import run_server

asyncio.run(run_server())
