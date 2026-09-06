"""Pluggy hook specifications for pynchy plugins.

This module defines the hook interface that plugins can implement to extend pynchy.
All hooks use the "pynchy" namespace and are validated by pluggy at registration time.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING

import pluggy

from pynchy.plugins.api import (
    Channel,
    RuntimeProvider,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)

if TYPE_CHECKING:
    from pynchy.actions.api import ActionSpec
    from pynchy.plugins.capabilities import HostActionRegistration
    from pynchy.plugins.channel_runtime import ChannelPluginContext
    from pynchy.plugins.computer_use import ComputerUseBackend
    from pynchy.plugins.connections import ConnectionRuntime
    from pynchy.plugins.contracts import (
        AgentCoreSpec,
        AgentHookSpec,
        JobSpec,
        McpServerSpec,
        WorkspaceSpec,
    )
    from pynchy.plugins.observers import ObserverProvider
    from pynchy.plugins.speech.api import SpeechSynthesizer
    from pynchy.plugins.tunnels.api import TunnelProvider
    from pynchy.plugins.webhooks import WebhookRoute

hookspec = pluggy.HookspecMarker("pynchy")


class PynchySpec:
    """Hook specifications for pynchy plugins.

    Plugins implement these hooks to provide agent cores, channels, service handlers,
    and skills. A single plugin can implement multiple hooks to provide multiple
    capabilities.
    """

    @hookspec
    def pynchy_container_runtime(self) -> RuntimeProvider | None:
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
    def pynchy_tunnel(self) -> TunnelProvider | None:
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
    def pynchy_agent_core_info(self) -> AgentCoreSpec:
        """Provide agent core implementation info.

        The agent core is the LLM framework that powers the agent (Claude SDK,
        OpenAI, Ollama, etc.). The returned specification provides everything needed to
        instantiate the core inside the container.
        """
        raise NotImplementedError

    @hookspec
    def pynchy_agent_hook_specs(self) -> tuple[AgentHookSpec, ...]:
        """Provide trusted agent lifecycle hook modules.

        Pynchy mounts each module read-only for container execution and passes
        the same module to direct-host runners. Built-in security hooks always
        run before plugin-provided ``before_tool_use`` handlers.
        """
        raise NotImplementedError

    @hookspec
    def pynchy_skill_paths(self) -> list[str]:
        """Provide paths to skill directories.

        Skills are markdown files that define agent capabilities (e.g., browser
        automation, code review patterns). The returned paths are mounted into
        the container and made available to the agent.

        Returns:
            List of absolute paths to skill directories
        """
        raise NotImplementedError

    @hookspec
    def pynchy_create_channel(
        self, context: ChannelPluginContext
    ) -> Channel | list[Channel] | None:
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
    def pynchy_connection_runtime(
        self,
    ) -> ConnectionRuntime | tuple[ConnectionRuntime, ...]:
        """Provide named external-provider connection runtimes.

        Connection runtimes own authenticated provider identities, durable
        polling or subscription lifecycles, and readiness. They do not become
        operator channels, so ordinary agent output never routes to them.
        """
        raise NotImplementedError

    @hookspec
    def pynchy_speech_synthesizer(self) -> SpeechSynthesizer | None:
        """Provide host-side synthesis for final spoken channel replies.

        Returns:
            Speech synthesizer with ``name``, ``synthesize()``, and ``health()``
            methods, or None if this plugin does not provide one.
        """

    @hookspec
    def pynchy_service_handler(
        self,
        computer_use_backends: tuple[ComputerUseBackend, ...],
    ) -> HostActionRegistration:
        """Provide host-side service tool handlers.

        Host-side handlers process IPC service requests from container MCP tools.
        Each handler receives the request data dict and returns a result or error.

        Args:
            computer_use_backends: Provider objects contributed through
                ``pynchy_computer_use_backend``. Implementations that do not
                compose computer-use providers omit this argument.

        Returns:
            A typed host-action registration.
        """
        raise NotImplementedError

    # Provider selection stays separate from the policy surface so every host
    # platform can retain one policy contract with its own backend.
    @hookspec
    def pynchy_computer_use_backend(self) -> ComputerUseBackend:
        """Provide a platform-specific implementation of the computer-use contract.

        The backend-neutral ``computer_use`` service owns policy and audit dispatch.
        Provider plugins contribute implementations here instead of registering
        competing host tools.
        """
        raise NotImplementedError

    @hookspec
    def pynchy_action_specs(self) -> tuple[ActionSpec, ...]:
        """Provide semantic action contracts owned by this plugin.

        Every host-action descriptor must name an ActionSpec whose agent-tool
        surface exposes the registered tool. Built-in and plugin specifications
        are validated together, so duplicate action IDs fail startup.
        """
        raise NotImplementedError

    @hookspec
    def pynchy_observer(self) -> ObserverProvider | None:
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
    def pynchy_before_context_reset(
        self,
        group: WorkspaceProfile,
    ) -> Awaitable[None]:
        """Settle one plugin-owned concern before destructive session cleanup."""
        raise NotImplementedError

    @hookspec
    def pynchy_mcp_server_spec(self) -> tuple[McpServerSpec, ...]:
        """Provide an MCP server specification.

        Plugin-provided MCP servers are merged with personalized definitions.
        Config.toml always wins if both define the same server name.

        Each contribution owns a name, parsed ``McpServerConfig``, and trust
        defaults. User config wins on name collisions.
        """
        raise NotImplementedError

    @hookspec
    def pynchy_workspace_spec(self) -> WorkspaceSpec:
        """Provide a managed workspace definition.

        Workspace plugins can ship periodic agents or preconfigured workspaces
        without requiring users to copy `[workspaces.*]` blocks manually.

        The contribution carries a folder and parsed ``WorkspaceConfig``.
        """
        raise NotImplementedError

    @hookspec
    def pynchy_job_specs(self) -> tuple[JobSpec, ...]:
        """Provide config-backed scheduled jobs from an external registry.

        Each contribution carries a name and parsed ``JobConfig``;
        personalized declarations win on name collisions.
        """
        raise NotImplementedError

    @hookspec
    def pynchy_webhook_routes(self) -> WebhookRoute | tuple[WebhookRoute, ...] | None:
        """Provide authenticated external event routes.

        Each route owns its provider schema and signature parser while the host
        enforces body, rate, workspace, receipt, and task-dispatch boundaries.
        """
