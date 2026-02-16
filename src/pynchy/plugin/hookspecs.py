"""Pluggy hook specifications for pynchy plugins.

This module defines the hook interface that plugins can implement to extend pynchy.
All hooks use the "pynchy" namespace and are validated by pluggy at registration time.
"""

from __future__ import annotations

from typing import Any

import pluggy

hookspec = pluggy.HookspecMarker("pynchy")


class PynchySpec:
    """Hook specifications for pynchy plugins.

    Plugins implement these hooks to provide agent cores, channels, MCP servers,
    and skills. A single plugin can implement multiple hooks to provide multiple
    capabilities.
    """

    @hookspec
    def pynchy_agent_core_info(self) -> dict[str, Any]:
        """Provide agent core implementation info.

        The agent core is the LLM framework that powers the agent (Claude SDK,
        OpenAI, Ollama, etc.). The returned dict provides everything needed to
        instantiate the core inside the container.

        Returns:
            Dict with keys:
                - name: Core identifier (e.g., "claude", "openai")
                - module: Fully qualified module path (e.g., "agent_runner.cores.claude")
                - class_name: Class name to instantiate (e.g., "ClaudeAgentCore")
                - packages: List of pip packages to install in container (e.g., ["openai>=1.0.0"])
                - host_source_path: Optional path to plugin source on host
                  (for mounting into container)
        """

    @hookspec
    def pynchy_mcp_server_spec(self) -> dict[str, Any]:
        """Provide MCP server specification.

        MCP (Model Context Protocol) servers provide tools that agents can use.
        The returned dict specifies how to start the MCP server inside the container.

        Returns:
            Dict with keys:
                - name: Server identifier (e.g., "filesystem", "web-search")
                - command: Command to run (e.g., "python", "node")
                - args: Command arguments (e.g., ["-m", "my_mcp_server"])
                - env: Environment variables (e.g., {"API_KEY": "..."})
                - host_source: Optional path to server source on host (for mounting into container)
        """

    @hookspec
    def pynchy_skill_paths(self) -> list[str]:
        """Provide paths to skill directories.

        Skills are markdown files that define agent capabilities (e.g., browser
        automation, code review patterns). The returned paths are mounted into
        the container and made available to the agent.

        Returns:
            List of absolute paths to skill directories
        """

    @hookspec
    def pynchy_create_channel(self, context: Any) -> Any | None:
        """Create a communication channel instance.

        Channels are long-running services that receive messages from external
        sources (WhatsApp, Discord, Slack) and route them to agents.

        Args:
            context: PluginContext with callbacks for message handling

        Returns:
            Channel instance implementing the Channel protocol, or None if this
            plugin doesn't provide channels
        """
