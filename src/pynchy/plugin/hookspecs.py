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
    def pynchy_container_runtime(self) -> Any | None:
        """Provide a container runtime implementation.

        Runtime plugins can return an object with:
            - name (str): runtime identifier (e.g., "apple")
            - cli (str): container CLI command (e.g., "container")
            - is_available() -> bool
            - ensure_running() -> None
            - list_running_containers(prefix: str) -> list[str]

        Returns:
            Runtime object, or None if this plugin doesn't provide one.
        """

    @hookspec
    def pynchy_tunnel(self) -> Any | None:
        """Provide a tunnel provider implementation.

        Tunnel plugins detect and report network tunnel connectivity
        (Tailscale, Cloudflare Tunnel, WireGuard, etc.).

        Returns:
            Tunnel provider object with:
                - name (str): tunnel identifier (e.g., "tailscale")
                - is_available() -> bool
                - is_connected() -> bool
                - status_summary() -> str
            Or None if this plugin doesn't provide one.
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
        sources and route them to agents.

        Args:
            context: PluginContext with callbacks for message handling

        Returns:
            Channel instance implementing the Channel protocol, or None if this
            plugin doesn't provide channels
        """

    @hookspec
    def pynchy_mcp_server_handler(self) -> dict[str, Any]:
        """Provide host-side MCP server handler for service tools.

        Host-side handlers process IPC service requests from container MCP tools.
        Each handler receives the request data dict and returns a result or error.

        Returns:
            Dict with keys:
                - tools: dict mapping tool_name → async handler function
                  Each handler takes (data: dict) and returns dict with "result" or "error"
        """

    @hookspec
    def pynchy_observer(self) -> Any | None:
        """Provide an event observer implementation.

        Observers subscribe to the EventBus and persist or process events
        (e.g., store to SQLite, forward to OpenTelemetry, write to log files).

        Returns:
            Observer object with:
                - name (str): observer identifier (e.g., "sqlite", "otel")
                - subscribe(event_bus: EventBus) -> None: attach listeners
                - close() -> coroutine: async teardown / flush
            Or None if this plugin doesn't provide one.
        """

    @hookspec
    def pynchy_memory(self) -> Any | None:
        """Provide a memory backend implementation.

        Returns:
            Memory provider object with:
                - name (str): backend identifier (e.g., "sqlite", "jsonl")
                - save(group_folder, key, content, category, metadata) -> dict
                - recall(group_folder, query, category, limit) -> list[dict]
                - forget(group_folder, key) -> dict
                - list_keys(group_folder, category) -> list[dict]
                - init() -> coroutine: async setup (create tables, etc.)
                - close() -> coroutine: async teardown
            Or None if this plugin doesn't provide one.
        """

    @hookspec
    def pynchy_workspace_spec(self) -> dict[str, Any]:
        """Provide a managed workspace definition.

        Workspace plugins can ship periodic agents or preconfigured workspaces
        without requiring users to copy `[workspaces.*]` blocks manually.

        Returns:
            Dict with keys:
                - folder: Workspace folder name (e.g., "code-improver")
                - config: WorkspaceConfig-compatible dict (schedule, prompt, etc.)
                - claude_md: Optional CLAUDE.md content to seed on first run
        """
