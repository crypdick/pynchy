"""Container runner — spawns agent execution in containers.

Spawns subprocesses, writes initial input as an IPC file (initial.json),
manages persistent sessions with IPC-based output streaming, and handles
activity-based timeouts.

This package is split into focused submodules:
  credentials    — Credential discovery and env file writing
  mounts         — Volume mount list and container arg construction
  process        — Process management, graceful stop, container removal
  session        — Persistent container sessions and registry
  orchestrator   — Container spawning
  mcp.resolution — MCP instance resolution (config expansion, kwargs, trust map)
"""

# Re-export public API so that `from pynchy.host.container_manager import X` works.
# Private helpers should be imported from their submodules directly.

from pynchy.agent_protocol.api import OnOutput
from pynchy.host.container_manager.credentials import has_api_credentials
from pynchy.host.container_manager.session import (
    ContainerSession,
    SessionDiedError,
    active_session_container_names,
    destroy_all_sessions,
    destroy_session,
    get_session,
    get_session_output_handler,
    start_session,
)

__all__ = [
    "ContainerSession",
    "OnOutput",
    "SessionDiedError",
    "active_session_container_names",
    "destroy_all_sessions",
    "destroy_session",
    "get_session",
    "get_session_output_handler",
    "has_api_credentials",
    "start_session",
]
