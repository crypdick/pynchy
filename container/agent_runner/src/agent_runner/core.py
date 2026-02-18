"""Provider-agnostic agent core protocol.

This module defines the interface for LLM agent frameworks (Claude SDK, OpenAI,
Ollama, LangChain, etc.). The main.py runner delegates to implementations of
this protocol, keeping framework-specific code isolated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class AgentCoreConfig:
    """Configuration for initializing an agent core.

    Attributes:
        cwd: Working directory for the agent (/workspace/group or /workspace/project)
        session_id: Optional session ID for resuming (core-specific semantics)
        group_folder: Group folder name
        chat_jid: Canonical chat identifier
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
    system_prompt_append: str | None = None
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    plugin_hooks: list[dict[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentEvent:
    """Event emitted during agent query execution.

    The type field determines which data keys are relevant:

    - "thinking": thinking (str)
    - "tool_use": tool_name (str), tool_input (dict)
    - "tool_result": tool_result_id (str), tool_result_content (str),
                     tool_result_is_error (bool)
    - "text": text (str)
    - "system": system_subtype (str), system_data (dict)
    - "result": result (str | None), result_metadata (dict)

    Not all cores emit all event types. For example, non-Claude cores may not
    emit "thinking" events unless using o1/o3 models.
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)


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

    async def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Execute a query and yield events.

        Args:
            prompt: User prompt or follow-up message

        Yields:
            AgentEvent instances with type-specific data

        Must yield at least one "result" event before returning.
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
