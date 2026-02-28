"""Built-in notebook execution MCP server plugin.

Registers a Docker-based MCP server that provides Jupyter notebook creation,
execution, and management. Kernel execution runs inside a sandboxed container
built from ``src/pynchy/agent/mcp/notebook.Dockerfile``.

Heavy dependencies (JupyterLab, ipykernel, FastMCP) are baked into the Docker
image — they never load in the main pynchy process.
"""

from __future__ import annotations

try:
    import pluggy

    hookimpl = pluggy.HookimplMarker("pynchy")
except ModuleNotFoundError:
    # Running inside the Docker container where only the MCP server is needed,
    # not the plugin registration machinery.
    pluggy = None  # type: ignore[assignment]
    hookimpl = lambda f: f  # noqa: E731 — no-op decorator


class NotebookServerPlugin:
    @hookimpl
    def pynchy_mcp_server_spec(self) -> dict:
        return {
            "name": "notebook",
            "type": "docker",
            "image": "pynchy-mcp-notebook:latest",
            "dockerfile": "src/pynchy/agent/mcp/notebook.Dockerfile",
            "args": ["--workspace-dir", "/workspace"],
            "port": 8460,
            "extra_ports": [8888],
            "transport": "streamable_http",
            "idle_timeout": 1800,  # 30 min — MCP manager stops idle containers
            "inject_workspace": True,  # auto-scope notebooks per workspace
            "volumes": ["groups/{workspace}:/workspace"],
        }
