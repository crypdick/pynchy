"""Container runner — spawns agent execution in containers.

Spawns subprocesses, writes initial input as an IPC file (initial.json),
manages persistent sessions with IPC-based output streaming, and handles
activity-based timeouts.

This package is split into focused submodules:
  _serialization  — JSON boundary crossing (ContainerInput <-> dict, output parsing)
  _credentials    — Credential discovery and env file writing
  _session_prep   — Session directory file preparation (skills, settings)
  _mounts         — Volume mount list and container arg construction
  _process        — Process management, graceful stop, container removal
  _snapshots      — IPC snapshot file helpers
  _session        — Persistent container sessions and registry
  _orchestrator   — Container spawning and agent core resolution
  _mcp_resolution — MCP instance resolution (config expansion, kwargs, trust map)
"""

# Re-export public API so that `from pynchy.container_runner import X` keeps working.
# Private helpers (_xxx) should be imported from their submodules directly.

from pynchy.container_runner._credentials import has_api_credentials
from pynchy.container_runner._orchestrator import (
    resolve_agent_core,
    resolve_container_timeout,
)
from pynchy.container_runner._process import OnOutput
from pynchy.container_runner._session import (
    ContainerSession,
    SessionDiedError,
    create_session,
    destroy_all_sessions,
    destroy_session,
    get_session,
    get_session_output_handler,
)
from pynchy.container_runner._snapshots import write_groups_snapshot, write_tasks_snapshot

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
