"""Built-in notebook execution MCP server plugin.

Registers a script-type MCP server that provides Jupyter notebook creation,
execution, and management via FastMCP + jupyter_client. The MCP server is a
standalone uv script with inline dependencies — heavy deps (JupyterLab,
ipykernel, FastMCP) never touch pynchy's virtualenv.

Agents use MCP tools (start_kernel, execute_cell, etc.) to build notebooks
interactively. Humans view results via JupyterLab on port 8888.

Workspace isolation: uses ``inject_workspace`` so each workspace gets
its own server instance automatically. Notebooks are scoped to
``data/notebooks/<workspace>/`` with no per-workspace config needed.
"""

from __future__ import annotations

from pathlib import Path

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")

# Standalone uv script with PEP 723 inline deps — not a package module.
_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "notebook_server.py"


class NotebookServerPlugin:
    @hookimpl
    def pynchy_mcp_server_spec(self) -> dict:
        return {
            "name": "notebook",
            "command": "uv",
            "args": ["run", str(_SCRIPT)],
            "port": 8460,
            "transport": "streamable_http",
            "idle_timeout": 1800,  # 30 min — matches internal idle logic
            "inject_workspace": True,  # auto-scope notebooks per workspace
        }
