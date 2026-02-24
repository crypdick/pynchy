"""Container runner — spawns agent execution in containers.

Spawns subprocesses, writes initial input as an IPC file (initial.json),
collects output from IPC output files, manages activity-based timeouts,
and writes log files.

This package is split into focused submodules:
  _serialization  — JSON boundary crossing (ContainerInput <-> dict, output parsing)
  _credentials    — Credential discovery and env file writing
  _session_prep   — Session directory file preparation (skills, settings)
  _mounts         — Volume mount list and container arg construction
  _process        — Process management, stderr reading, timeout handling
  _logging        — Run log file writing
  _snapshots      — IPC snapshot file helpers
  _session        — Persistent container sessions and registry
  _orchestrator   — Main entry point (run_container_agent) and agent core resolution
"""

# Re-export public API so that `from pynchy.container_runner import X` keeps working.
# Private helpers (_xxx) should be imported from their submodules directly.

from pynchy.container_runner._credentials import _write_env_file
from pynchy.container_runner._orchestrator import OnOutput, OnProcess, resolve_agent_core
from pynchy.container_runner._process import _graceful_stop, read_stderr
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
    "OnProcess",
    "SessionDiedError",
    "_graceful_stop",
    "_write_env_file",
    "create_session",
    "destroy_all_sessions",
    "destroy_session",
    "get_session",
    "get_session_output_handler",
    "read_stderr",
    "resolve_agent_core",
    "write_groups_snapshot",
    "write_tasks_snapshot",
]
