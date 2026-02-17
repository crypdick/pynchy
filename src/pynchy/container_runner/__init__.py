"""Container runner — spawns agent execution in containers.

Spawns subprocesses, writes JSON input to stdin,
parses streaming output using sentinel markers, manages activity-based timeouts,
and writes log files.

This package is split into focused submodules:
  _serialization  — JSON boundary crossing (ContainerInput <-> dict, output parsing)
  _credentials    — Credential discovery and env file writing
  _session_prep   — Session directory file preparation (skills, settings)
  _mounts         — Volume mount list and container arg construction
  _process        — Process management, I/O streaming, timeout handling
  _logging        — Run log file writing and legacy output parsing
  _snapshots      — IPC snapshot file helpers
  _orchestrator   — Main entry point (run_container_agent) and agent core resolution
"""

# Re-export every public symbol so that `from pynchy.container_runner import X` keeps working.

from pynchy.container_runner._credentials import (
    _read_gh_token,
    _read_git_identity,
    _read_oauth_from_keychain,
    _read_oauth_token,
    _shell_quote,
    _write_env_file,
)
from pynchy.container_runner._logging import (
    RunContext,
    _parse_final_output,
    _write_run_log,
)
from pynchy.container_runner._mounts import (
    _build_container_args,
    _build_volume_mounts,
)
from pynchy.container_runner._orchestrator import (
    OnOutput,
    OnProcess,
    _collect_plugin_mcp_specs,
    _determine_result,
    resolve_agent_core,
)
from pynchy.container_runner._process import (
    StreamState,
    _graceful_stop,
    read_stderr,
    read_stdout,
)
from pynchy.container_runner._serialization import (
    _input_to_dict,
    _parse_container_output,
)
from pynchy.container_runner._session_prep import (
    _is_skill_selected,
    _parse_skill_tier,
    _sync_skills,
    _write_settings_json,
)
from pynchy.container_runner._snapshots import (
    write_groups_snapshot,
    write_tasks_snapshot,
)

__all__ = [
    # credentials
    "_read_gh_token",
    "_read_git_identity",
    "_read_oauth_from_keychain",
    "_read_oauth_token",
    "_shell_quote",
    "_write_env_file",
    # logging
    "RunContext",
    "_parse_final_output",
    "_write_run_log",
    # mounts
    "_build_container_args",
    "_build_volume_mounts",
    # orchestrator
    "OnOutput",
    "OnProcess",
    "_collect_plugin_mcp_specs",
    "_determine_result",
    "resolve_agent_core",
    # process
    "StreamState",
    "_graceful_stop",
    "read_stderr",
    "read_stdout",
    # serialization
    "_input_to_dict",
    "_parse_container_output",
    # session_prep
    "_is_skill_selected",
    "_parse_skill_tier",
    "_sync_skills",
    "_write_settings_json",
    # snapshots
    "write_groups_snapshot",
    "write_tasks_snapshot",
]
