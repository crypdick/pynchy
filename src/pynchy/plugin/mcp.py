"""MCP plugin system for agent tools.

Enables agent tools (MCP servers) to be provided by external plugins.
Plugins package their MCP server code and declare how to run it inside
the agent container.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from pynchy.plugin.base import PluginBase


@dataclass
class McpServerSpec:
    """Specification for running an MCP server inside the agent container.

    The MCP server runs in the container and provides tools to the agent.
    Plugin source code is mounted into the container so the server can be imported.
    """

    name: str
    """MCP server name (e.g., 'voice', 'calendar'). Must be unique."""

    command: str
    """Command to run inside the container (e.g., 'python', 'node')."""

    args: list[str]
    """Command arguments (e.g., ['-m', 'pynchy_plugin_voice.mcp'])."""

    env: dict[str, str] = field(default_factory=dict)
    """Extra environment variables for the MCP server process."""

    host_source: Path | None = None
    """Path to plugin package directory on host to mount into container.

    Typically Path(__file__).parent to mount the plugin's source directory.
    Mounted to /workspace/plugins/{name}/ inside the container.
    """


class McpPlugin(PluginBase):
    """Base class for MCP tool plugins.

    MCP plugins provide agent tools by running MCP servers inside the
    agent container. The plugin source is mounted into the container
    and the server is configured via the spec.

    .. note:: **Mostly sandboxed — lower risk.**

       The ``mcp_server_spec()`` method runs briefly on the host, but the
       MCP server itself runs inside the container with the plugin source
       mounted **read-only**. This is the most isolated plugin category.
       However, the host-side plugin class (``__init__``, ``validate()``,
       ``mcp_server_spec()``) still executes with full host privileges.

       **Only install plugins from authors you trust.**
    """

    categories = ["mcp"]  # Fixed category for all MCP plugins

    @abstractmethod
    def mcp_server_spec(self) -> McpServerSpec:
        """Return the MCP server specification.

        Called during plugin discovery. The spec defines how to run the
        MCP server inside the agent container.

        Returns:
            McpServerSpec: Configuration for running the MCP server
        """
        ...
