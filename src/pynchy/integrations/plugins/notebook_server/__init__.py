"""Built-in notebook execution MCP server plugin.

Registers a script-type MCP server that provides Jupyter notebook creation,
execution, and management. The server runs as a subprocess via
``python -m pynchy.integrations.plugins.notebook_server``.

Heavy dependencies (JupyterLab, ipykernel, FastMCP) are declared as the
``pynchy[notebook]`` optional extra — they only load in the subprocess,
never in the main pynchy process.
"""

from __future__ import annotations

import sys

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")


class NotebookServerPlugin:
    @hookimpl
    def pynchy_mcp_server_spec(self) -> dict:
        return {
            "name": "notebook",
            "command": sys.executable,
            "args": ["-m", "pynchy.integrations.plugins.notebook_server"],
            "port": 8460,
            "transport": "streamable_http",
            "idle_timeout": 1800,  # 30 min — matches internal idle logic
            "inject_workspace": True,  # auto-scope notebooks per workspace
        }
