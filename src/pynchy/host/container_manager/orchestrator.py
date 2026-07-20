"""Container spawning and agent core resolution.

Provides ``_spawn_container()`` (shared by cold-start and scheduled-task
paths in ``agent_runner``) and ``resolve_agent_core()`` (plugin lookup).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import (
    Path,  # noqa: TC003, RUF100 - beartype resolves container orchestration signatures at runtime.
)

import pluggy  # noqa: TC002, RUF100 - beartype resolves agent core lookup signatures at runtime.

from pynchy.config import get_settings
from pynchy.host.container_manager.mcp.startup import (  # noqa: TC001, RUF100 - beartype resolves container orchestration signatures at runtime.
    McpStartupFailure,
)
from pynchy.host.container_manager.mounts import build_container_args, build_volume_mounts
from pynchy.host.container_manager.runtime_names import runtime_container_name
from pynchy.host.container_manager.serialization import input_to_dict
from pynchy.host.git_ops.repo import (
    RepoContext,  # noqa: TC001, RUF100 - beartype resolves container orchestration signatures at runtime.
)
from pynchy.logger import logger
from pynchy.plugins.runtimes import system_checks
from pynchy.plugins.runtimes.detection import get_runtime
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves container orchestration signatures at runtime.
    ContainerInput,
    VolumeMount,
    WorkspaceProfile,
)

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
    """Convert non-alphanumeric/non-dash chars to dashes for container names."""
    return "".join(c if c.isalnum() or c == "-" else "-" for c in group_folder)


def stable_container_name(group_folder: str) -> str:
    """Deterministic container name for persistent sessions.

    Using a stable name means we can docker rm -f the stale container
    before spawning a fresh one for the same group.
    """
    return runtime_container_name(_sanitize_folder(group_folder))


def oneshot_container_name(group_folder: str) -> str:
    """Timestamped container name for one-shot runs (scheduled tasks)."""
    return runtime_container_name(f"{_sanitize_folder(group_folder)}-{int(time.time() * 1000)}")


# ---------------------------------------------------------------------------
# Agent core resolution
# ---------------------------------------------------------------------------


def resolve_agent_core(plugin_manager: pluggy.PluginManager | None) -> tuple[str, str]:
    """Look up the agent core module and class from plugins.

    Returns (module_path, class_name) for the configured agent core.
    Falls back to the defaults in ContainerInput if no plugin provides one.
    """
    module = "agent_runner.cores.openai"
    class_name = "OpenAIAgentCore"
    if plugin_manager:
        cores = plugin_manager.hook.pynchy_agent_core_info()
        core_info = next(
            (c for c in cores if c["name"] == get_settings().agent.default_core),
            None,
        )
        if core_info is None and cores:
            core_info = cores[0]
        if core_info:
            module = core_info["module"]
            class_name = core_info["class_name"]
    return module, class_name


# ---------------------------------------------------------------------------
# Initial input file
# ---------------------------------------------------------------------------


def write_initial_input(input_data: ContainerInput, input_dir: Path) -> None:
    """Write ContainerInput as initial.json for the container to read on startup.

    Uses atomic write (write to .tmp then rename) so the container's file
    watcher never sees a partially-written file.
    """
    from pynchy.utils import (  # noqa: PLC0415, RUF100 - keep container orchestration import surface narrow at module load.
        write_json_atomic,
    )

    write_json_atomic(input_dir / "initial.json", input_to_dict(input_data))


# ---------------------------------------------------------------------------
# Shared spawn logic
# ---------------------------------------------------------------------------


async def _spawn_container(
    group: WorkspaceProfile,
    input_data: ContainerInput,
    container_name: str,
    plugin_manager: pluggy.PluginManager | None = None,
) -> tuple[asyncio.subprocess.Process, str, list[VolumeMount], tuple[McpStartupFailure, ...]]:
    """Resolve environment, build mounts, and spawn a container subprocess.

    Shared by the cold-start and scheduled-task paths in ``agent_runner``.
    Returns (proc, container_name, mounts, mcp_startup_failures).

    Raises OSError if the subprocess fails to start.
    """
    start_time = time.monotonic()

    # The harness deliberately defers this expensive check at host startup,
    # but an agent container must never be spawned without its image.
    await asyncio.to_thread(system_checks.ensure_agent_image_available)

    # Create session-scoped SecurityGate keyed by (group_folder, invocation_ts).
    # Must exist before the container starts so IPC/MCP handlers can look it up.
    from pynchy.host.container_manager.security.gate import (  # noqa: PLC0415, RUF100 - security gate setup is only needed during container spawn.
        create_gate,
        resolve_security,
    )

    security = resolve_security(group.folder, is_admin=input_data.is_admin)
    invocation_ts = start_time
    create_gate(
        group.folder,
        invocation_ts,
        security,
        public_source_input=input_data.input_source.startswith(("webhook:", "external:")),
        secret_source_input=input_data.input_source == "external:matrix",
    )
    input_data.invocation_ts = invocation_ts

    s = get_settings()
    group_dir = s.groups_dir / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    # --- Resolve worktree ---
    phase_start = time.monotonic()
    repo_mounts: list[tuple[RepoContext, Path]] = []
    if input_data.repo_accesses:
        from pynchy.host.git_ops.repo import (  # noqa: PLC0415, RUF100 - git worktree setup is only needed for repo-enabled spawns.
            get_repo_context,
            resolve_repos_for_group,
        )
        from pynchy.host.git_ops.worktree import (  # noqa: PLC0415, RUF100 - git worktree setup is only needed for repo-enabled spawns.
            ensure_worktree,
        )

        repo_contexts = resolve_repos_for_group(group.folder)
        if not repo_contexts:
            repo_contexts = [
                repo_ctx
                for slug in input_data.repo_accesses
                if (repo_ctx := get_repo_context(slug)) is not None
            ]
        for repo_ctx in repo_contexts:
            wt_result = ensure_worktree(group.folder, repo_ctx)
            repo_mounts.append((repo_ctx, wt_result.path))
            if wt_result.notices:
                if input_data.system_notices is None:
                    input_data.system_notices = []
                input_data.system_notices.extend(wt_result.notices)
    worktree_ms = (time.monotonic() - phase_start) * 1000

    # --- Build mounts ---
    phase_start = time.monotonic()
    mounts = build_volume_mounts(
        group,
        is_admin=input_data.is_admin,
        plugin_manager=plugin_manager,
        repo_mounts=repo_mounts,
    )
    mounts_ms = (time.monotonic() - phase_start) * 1000

    # --- MCP gateway: ensure containers running and pass credentials ---
    phase_start = time.monotonic()
    from pynchy.host.container_manager.mcp.manager import (  # noqa: PLC0415, RUF100 - MCP manager is only needed during container spawn.
        get_mcp_manager,
    )

    mcp_mgr = get_mcp_manager()
    mcp_instance_count = 0
    mcp_startup_failures: tuple[McpStartupFailure, ...] = ()
    if mcp_mgr is not None:
        mcp_instance_count = len(mcp_mgr.get_workspace_instance_ids(group.folder))
        mcp_startup = await mcp_mgr.ensure_workspace_running(group.folder)
        mcp_startup_failures = mcp_startup.failures

        # Route MCP traffic through the security proxy so SecurityGate can
        # enforce policy and apply fencing on responses from untrusted sources.
        direct_configs = mcp_mgr.get_direct_server_configs(
            group.folder,
            invocation_ts=input_data.invocation_ts,
            instance_ids=mcp_startup.ready_instance_ids,
        )
        if direct_configs:
            input_data.mcp_direct_servers = direct_configs
    mcp_ms = (time.monotonic() - phase_start) * 1000

    # --- Build args ---
    container_args = build_container_args(mounts, container_name)

    # --- Write initial input as file (container reads on startup) ---
    ipc_input_dir = s.data_dir / "ipc" / group.folder / "input"
    write_initial_input(input_data, ipc_input_dir)

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

    return proc, container_name, mounts, mcp_startup_failures
