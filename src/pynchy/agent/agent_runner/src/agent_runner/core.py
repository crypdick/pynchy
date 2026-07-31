"""Provider-agnostic agent core protocol.

This module defines the interface for LLM agent frameworks (Claude SDK, OpenAI,
Ollama, LangChain, etc.). The main.py runner delegates to implementations of
this protocol, keeping framework-specific code isolated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_runner.events import AgentEvent


@dataclass
class AgentCoreConfig:
    """Configuration for initializing an agent core.

    Attributes:
        cwd: Working directory for the agent (/home/agent/workspace or /home/agent/src/owner/repo)
        session_id: Optional session ID for resuming (core-specific semantics)
        group_folder: Group folder name
        chat_jid: Canonical chat identifier
        turn_id: Conversation turn identifier for host/Phoenix correlation
        is_admin: Whether this is the admin group
        is_scheduled_task: Whether this is a scheduled task (vs interactive message)
        system_prompt_append: Additional system context (global CLAUDE.md + system notices)
        mcp_servers: MCP server configurations {name: {command, args, env}}
        plugin_hooks: Hook configurations [{name, module_path}]
        extra: Core-specific configuration (model name, API keys, etc.)
    """

    cwd: str
    session_id: str | None
    group_folder: str
    chat_jid: str
    is_admin: bool
    is_scheduled_task: bool
    turn_id: str | None = None
    system_prompt_append: str | None = None
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    plugin_hooks: list[dict[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AgentCore(Protocol):
    """Protocol for LLM agent framework implementations.

    Implementations provide:
    - Lifecycle management (start/stop for resource acquisition)
    - Query processing (prompt → event stream)
    - Session management (opaque to the runner, read via session_id property)

    The runner calls start() before first query, yields events from query(),
    and calls stop() at shutdown. Session state is managed internally by the
    core and exposed via the session_id property.
    """

    async def start(self) -> None:
        """Initialize the agent core (acquire resources, start clients, etc.)."""
        ...

    def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Execute a query and yield events.

        Args:
            prompt: User prompt or follow-up message

        Yields:
            AgentEvent instances with type-specific data

        Consumers validate the stream and require exactly one terminal result.
        """
        ...

    async def stop(self) -> None:
        """Clean up resources (close clients, save state, etc.)."""
        ...

    @property
    def session_id(self) -> str | None:
        """Current session identifier (core-specific format).

        Returns None if no session is active. The runner reads this after
        each query to track session state across IPC messages.
        """
        ...
