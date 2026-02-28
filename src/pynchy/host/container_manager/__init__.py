"""Container runner — spawns agent execution in containers.

Spawns subprocesses, writes initial input as an IPC file (initial.json),
manages persistent sessions with IPC-based output streaming, and handles
activity-based timeouts.

This package is split into focused submodules:
  serialization  — JSON boundary crossing (ContainerInput <-> dict, output parsing)
  credentials    — Credential discovery and env file writing
  session_prep   — Session directory file preparation (skills, settings)
  mounts         — Volume mount list and container arg construction
  process        — Process management, graceful stop, container removal
  snapshots      — IPC snapshot file helpers
  session        — Persistent container sessions and registry
  orchestrator   — Container spawning and agent core resolution
  mcp.resolution — MCP instance resolution (config expansion, kwargs, trust map)
"""

# Re-export public API so that `from pynchy.host.container_manager import X` works.
# Private helpers should be imported from their submodules directly.

from pynchy.host.container_manager.credentials import has_api_credentials
from pynchy.host.container_manager.orchestrator import (
    resolve_agent_core,
    resolve_container_timeout,
)
from pynchy.host.container_manager.process import OnOutput
from pynchy.host.container_manager.session import (
    ContainerSession,
    SessionDiedError,
    create_session,
    destroy_all_sessions,
    destroy_session,
    get_session,
    get_session_output_handler,
)
from pynchy.host.container_manager.snapshots import write_groups_snapshot, write_tasks_snapshot

__all__ = [
    "ContainerSession",
    "OnOutput",
    "SessionDiedError",
    "has_api_credentials",
    "create_session",
    "destroy_all_sessions",
    "destroy_session",
    "get_session",
    "get_session_output_handler",
    "resolve_agent_core",
    "resolve_container_timeout",
    "write_groups_snapshot",
    "write_tasks_snapshot",
]
