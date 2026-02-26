"""Container spawning and agent core resolution.

Provides ``_spawn_container()`` (shared by cold-start and scheduled-task
paths in ``agent_runner``) and ``resolve_agent_core()`` (plugin lookup).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pluggy

from pynchy.config import get_settings
from pynchy.container_runner._mounts import _build_container_args, _build_volume_mounts
from pynchy.container_runner._serialization import _input_to_dict
from pynchy.logger import logger
from pynchy.runtime.runtime import get_runtime
from pynchy.types import ContainerInput, VolumeMount, WorkspaceProfile

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


def _sanitize_folder(group_folder: str) -> str:
    """Replace non-alphanumeric/non-dash chars with dashes for container names."""
    return "".join(c if c.isalnum() or c == "-" else "-" for c in group_folder)


def stable_container_name(group_folder: str) -> str:
    """Deterministic container name for persistent sessions.

    Using a stable name means we can docker rm -f the stale container
    before spawning a new one for the same group.
    """
    return f"pynchy-{_sanitize_folder(group_folder)}"


def oneshot_container_name(group_folder: str) -> str:
    """Timestamped container name for one-shot runs (scheduled tasks)."""
    return f"pynchy-{_sanitize_folder(group_folder)}-{int(time.time() * 1000)}"


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
# Initial input file
# ---------------------------------------------------------------------------


def _write_initial_input(input_data: ContainerInput, input_dir: Path) -> None:
    """Write ContainerInput as initial.json for the container to read on startup.

    Uses atomic write (write to .tmp then rename) so the container's file
    watcher never sees a partially-written file.
    """
    from pynchy.utils import write_json_atomic

    write_json_atomic(input_dir / "initial.json", _input_to_dict(input_data))


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

    Shared by the cold-start and scheduled-task paths in ``agent_runner``.
    Returns (proc, container_name, mounts).

    Raises OSError if the subprocess fails to start.
    """
    start_time = time.monotonic()

    # Create session-scoped SecurityGate keyed by (group_folder, invocation_ts).
    # Must exist before the container starts so IPC/MCP handlers can look it up.
    from pynchy.security.gate import create_gate, resolve_security

    security = resolve_security(group.folder, is_admin=input_data.is_admin)
    invocation_ts = start_time
    create_gate(group.folder, invocation_ts, security)
    input_data.invocation_ts = invocation_ts

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

        # Route MCP traffic through the security proxy so SecurityGate can
        # enforce policy and apply fencing on responses from untrusted sources.
        direct_configs = mcp_mgr.get_direct_server_configs(
            group.folder, invocation_ts=input_data.invocation_ts
        )
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
