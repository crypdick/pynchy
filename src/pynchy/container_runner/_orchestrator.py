"""Main entry point — spawns container agent, manages lifecycle, returns result.

Also contains agent core resolution (plugin lookup).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pluggy

from pynchy.config import get_settings
from pynchy.container_runner._logging import _write_run_log
from pynchy.container_runner._mounts import _build_container_args, _build_volume_mounts
from pynchy.container_runner._process import (
    _classify_exit,
    _wait_for_exit,
)
from pynchy.container_runner._serialization import _input_to_dict, _parse_container_output
from pynchy.logger import logger
from pynchy.runtime.runtime import get_runtime
from pynchy.types import ContainerInput, ContainerOutput, VolumeMount, WorkspaceProfile

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

OnProcess = Callable[[asyncio.subprocess.Process, str], Any]
OnOutput = Callable[[ContainerOutput], Awaitable[None]]


# ---------------------------------------------------------------------------
# Container timeout resolution
# ---------------------------------------------------------------------------


def resolve_container_timeout(group: WorkspaceProfile) -> float:
    """Return the effective container timeout in seconds.

    Per-workspace ``container_config.timeout`` takes priority; falls back to
    the global ``container.timeout_ms`` from Settings (converted to seconds).
    """
    if group.container_config and group.container_config.timeout:
        return group.container_config.timeout
    return get_settings().container_timeout


# ---------------------------------------------------------------------------
# Container name helpers
# ---------------------------------------------------------------------------


def stable_container_name(group_folder: str) -> str:
    """Deterministic container name for persistent sessions.

    Using a stable name means we can docker rm -f the stale container
    before spawning a new one for the same group.
    """
    safe_name = "".join(c if c.isalnum() or c == "-" else "-" for c in group_folder)
    return f"pynchy-{safe_name}"


def oneshot_container_name(group_folder: str) -> str:
    """Timestamped container name for one-shot runs (scheduled tasks)."""
    safe_name = "".join(c if c.isalnum() or c == "-" else "-" for c in group_folder)
    return f"pynchy-{safe_name}-{int(time.time() * 1000)}"


# ---------------------------------------------------------------------------
# Agent core resolution
# ---------------------------------------------------------------------------


def resolve_agent_core(plugin_manager: pluggy.PluginManager | None) -> tuple[str, str]:
    """Look up the agent core module and class from plugins.

    Returns (module_path, class_name) for the configured agent core.
    Falls back to the defaults in ContainerInput if no plugin provides one.
    """
    module = "agent_runner.cores.claude"
    class_name = "ClaudeAgentCore"
    if plugin_manager:
        cores = plugin_manager.hook.pynchy_agent_core_info()
        core_info = next((c for c in cores if c["name"] == get_settings().agent.core), None)
        if core_info is None and cores:
            core_info = cores[0]
        if core_info:
            module = core_info["module"]
            class_name = core_info["class_name"]
    return module, class_name


# ---------------------------------------------------------------------------
# IPC output directory reading
# ---------------------------------------------------------------------------


def _read_output_files(output_dir: Path, group_name: str) -> list[ContainerOutput]:
    """Read and parse all output event files from the IPC output directory.

    Files are sorted by name (monotonic nanosecond timestamps) to preserve
    ordering.  Each file is deleted after successful parsing.

    Returns a list of parsed ContainerOutput events.
    """
    outputs: list[ContainerOutput] = []
    if not output_dir.exists():
        return outputs

    for file_path in sorted(f for f in output_dir.iterdir() if f.suffix == ".json"):
        try:
            json_str = file_path.read_text()
            parsed = _parse_container_output(json_str)
            outputs.append(parsed)
            file_path.unlink()
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.warning(
                "Failed to parse output file",
                group=group_name,
                file=file_path.name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            # Leave the file for debugging; don't block other files
    return outputs


# ---------------------------------------------------------------------------
# Initial input file
# ---------------------------------------------------------------------------


def _write_initial_input(input_data: ContainerInput, input_dir: Path) -> None:
    """Write ContainerInput as initial.json for the container to read on startup.

    Uses atomic write (write to .tmp then rename) so the container's file
    watcher never sees a partially-written file.
    """
    input_dir.mkdir(parents=True, exist_ok=True)
    filepath = input_dir / "initial.json"
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(_input_to_dict(input_data)))
    temp_path.rename(filepath)


# ---------------------------------------------------------------------------
# Shared spawn logic
# ---------------------------------------------------------------------------


async def _spawn_container(
    group: WorkspaceProfile,
    input_data: ContainerInput,
    container_name: str,
    plugin_manager: pluggy.PluginManager | None = None,
) -> tuple[asyncio.subprocess.Process, str, list[VolumeMount]]:
    """Resolve environment, build mounts, and spawn a container subprocess.

    Shared by both one-shot run_container_agent() and the persistent session
    cold-start path.  Returns (proc, container_name, mounts).

    Raises OSError if the subprocess fails to start.
    """
    start_time = time.monotonic()
    s = get_settings()
    group_dir = s.groups_dir / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    # --- Resolve worktree ---
    phase_start = time.monotonic()
    worktree_path: Path | None = None
    repo_ctx = None
    if input_data.repo_access:
        from pynchy.git_ops.repo import resolve_repo_for_group
        from pynchy.git_ops.worktree import ensure_worktree

        repo_ctx = resolve_repo_for_group(group.folder)
        if repo_ctx is not None:
            wt_result = ensure_worktree(group.folder, repo_ctx)
            worktree_path = wt_result.path
            if wt_result.notices:
                if input_data.system_notices is None:
                    input_data.system_notices = []
                input_data.system_notices.extend(wt_result.notices)
    worktree_ms = (time.monotonic() - phase_start) * 1000

    # --- Build mounts ---
    phase_start = time.monotonic()
    mounts = _build_volume_mounts(
        group, input_data.is_admin, plugin_manager, repo_ctx, worktree_path
    )
    mounts_ms = (time.monotonic() - phase_start) * 1000

    # --- MCP gateway: ensure containers running and pass credentials ---
    phase_start = time.monotonic()
    from pynchy.container_runner.mcp_manager import get_mcp_manager

    mcp_mgr = get_mcp_manager()
    mcp_instance_count = 0
    if mcp_mgr is not None:
        mcp_instance_count = len(mcp_mgr.get_workspace_instance_ids(group.folder))
        await mcp_mgr.ensure_workspace_running(group.folder)

        # Provide direct MCP server URLs (bypasses LiteLLM MCP proxy which
        # doesn't work with Claude SDK — see backlog/3-ready/mcp-gateway-transport.md).
        direct_configs = mcp_mgr.get_direct_server_configs(group.folder)
        if direct_configs:
            input_data.mcp_direct_servers = direct_configs
    mcp_ms = (time.monotonic() - phase_start) * 1000

    # --- Build args ---
    container_args = _build_container_args(mounts, container_name)

    # --- Write initial input as file (container reads on startup) ---
    ipc_input_dir = s.data_dir / "ipc" / group.folder / "input"
    _write_initial_input(input_data, ipc_input_dir)

    pre_spawn_ms = (time.monotonic() - start_time) * 1000
    logger.info(
        "Spawning container agent",
        group=group.name,
        container=container_name,
        mount_count=len(mounts),
        is_admin=input_data.is_admin,
        worktree_ms=round(worktree_ms),
        mounts_ms=round(mounts_ms),
        mcp_ms=round(mcp_ms),
        mcp_instances=mcp_instance_count,
        pre_spawn_ms=round(pre_spawn_ms),
    )

    # --- Spawn process (stdin not needed — input delivered via IPC file) ---
    proc = await asyncio.create_subprocess_exec(
        get_runtime().cli,
        *container_args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    return proc, container_name, mounts


# ---------------------------------------------------------------------------
# One-shot entry point (scheduled tasks)
# ---------------------------------------------------------------------------


async def run_container_agent(
    group: WorkspaceProfile,
    input_data: ContainerInput,
    on_process: OnProcess,
    on_output: OnOutput | None = None,
    plugin_manager: pluggy.PluginManager | None = None,
) -> ContainerOutput:
    """Spawn a container agent, wait for exit, and collect output from IPC files.

    Used for one-shot runs (scheduled tasks).  For interactive messages,
    use the persistent session path in agent_runner.run_agent().

    Output is collected from IPC output files written by the container, not
    from stdout.  Stdout is consumed and discarded (container logs to stderr).
    The on_output callback, if provided, is invoked for each output event
    after the container exits.

    Args:
        group: The registered group configuration.
        input_data: Input payload for the agent-runner.
        on_process: Callback invoked with (proc, container_name) after spawn.
        on_output: If provided, called for each output event after container exit.
        plugin_manager: Optional pluggy.PluginManager for plugin MCP mounts and config.

    Returns:
        ContainerOutput with the final status.
    """
    s = get_settings()

    container_name = oneshot_container_name(group.folder)

    logs_dir = s.groups_dir / group.folder / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    try:
        proc, container_name, mounts = await _spawn_container(
            group, input_data, container_name, plugin_manager
        )
    except OSError as exc:
        logger.error("Failed to spawn container", error=str(exc), container=container_name)
        return ContainerOutput(status="error", result=None, error=f"Spawn failed: {exc}")

    on_process(proc, container_name)

    # --- Wait for container exit (timeout, I/O drain, cleanup) ---
    config_timeout = resolve_container_timeout(group)
    # Grace period: hard timeout must be at least idle_timeout + 30s
    timeout_secs = max(config_timeout, s.idle_timeout + 30.0)

    exit_info = await _wait_for_exit(
        proc, container_name, group.name, timeout_secs, s.container.max_output_size
    )

    # --- Collect output from IPC files ---
    ipc_output_dir = s.data_dir / "ipc" / group.folder / "output"
    outputs = _read_output_files(ipc_output_dir, group.name)

    # Deliver output events via callback
    if on_output is not None:
        for output_event in outputs:
            try:
                await on_output(output_event)
            except Exception as exc:
                logger.error(
                    "Output callback failed",
                    group=group.name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    # --- Write log ---
    container_args = _build_container_args(mounts, container_name)
    _write_run_log(
        logs_dir=logs_dir,
        group_name=group.name,
        container_name=container_name,
        input_data=input_data,
        container_args=container_args,
        mounts=mounts,
        stderr=exit_info.stderr,
        duration_ms=exit_info.duration_ms,
        exit_code=exit_info.exit_code,
        timed_out=exit_info.timed_out,
        output_event_count=len(outputs),
    )

    return _classify_exit(exit_info, outputs, group.name, container_name, config_timeout)
