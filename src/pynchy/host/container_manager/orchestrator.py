"""Container spawning and agent core resolution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import (
    Path,  # beartype resolves container orchestration signatures at runtime.
)

import pluggy  # noqa: TC002 - beartype resolves agent core lookup signatures at runtime.

from pynchy.agent_protocol.api import (
    # beartype resolves container orchestration signatures at runtime.
    AgentExecutionRuntime,
    ContainerInput,
    McpStartupFailure,
    input_to_dict,
)
from pynchy.host.container_manager.contracts import RepoMount, RepoMountResolution
from pynchy.host.container_manager.credentials import build_agent_env_vars
from pynchy.host.container_manager.mounts import build_container_args, build_volume_mounts
from pynchy.host.paths import (
    AGENT_AUTOMATION_MEMORY_CONTAINER_PATH,
    PERSONALIZATION_SKILLS_CONTAINER_PATH,
    PYNCHY_IPC_CONTAINER_PATH,
)
from pynchy.logger import logger
from pynchy.plugins.api import collect_agent_hook_specs, container_agent_hook_configs
from pynchy.process_environment import filtered_process_environment
from pynchy.runtime_names import runtime_container_name
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves container orchestration signatures at runtime.
)

type EnsureAgentImage = Callable[..., None]
type ResolveRepoMounts = Callable[[str, tuple[str, ...]], RepoMountResolution]

_container_cli: str | None = None
_ensure_agent_image: EnsureAgentImage | None = None
_resolve_repo_mounts: ResolveRepoMounts | None = None


def configure_container_spawn_runtime(
    *,
    container_cli: str,
    ensure_agent_image: EnsureAgentImage,
    resolve_repo_mounts: ResolveRepoMounts,
) -> None:
    """Inject the selected container runtime at host composition."""
    global _container_cli, _ensure_agent_image, _resolve_repo_mounts  # noqa: PLW0603 - one host process owns one spawn runtime.
    _container_cli = container_cli
    _ensure_agent_image = ensure_agent_image
    _resolve_repo_mounts = resolve_repo_mounts


def _configured_container_runtime() -> tuple[str, EnsureAgentImage, ResolveRepoMounts]:
    if _container_cli is None or _ensure_agent_image is None or _resolve_repo_mounts is None:
        raise RuntimeError("container spawn runtime has not been configured")
    return _container_cli, _ensure_agent_image, _resolve_repo_mounts


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


# ---------------------------------------------------------------------------
# Initial input file
# ---------------------------------------------------------------------------


def write_initial_input(input_data: ContainerInput, input_dir: Path) -> None:
    """Write ContainerInput as initial.json for the container to read on startup.

    Uses atomic write (write to .tmp then rename) so the container's file
    watcher never sees a partially-written file.
    """
    from pynchy.atomic_json import (  # noqa: PLC0415 - keep container orchestration import surface narrow at module load.
        write_json_atomic,
    )

    write_json_atomic(input_dir / "initial.json", input_to_dict(input_data))


def _container_agent_environment(
    group: WorkspaceProfile,
    input_data: ContainerInput,
) -> dict[str, str]:
    environment = build_agent_env_vars(
        is_admin=input_data.is_admin,
        group_folder=group.folder,
    )
    environment.update(
        {
            "PYNCHY_GROUP_FOLDER": group.folder,
            "PYNCHY_IS_ADMIN": "1" if input_data.is_admin else "0",
            "PYNCHY_SKILLS_ROOT": PERSONALIZATION_SKILLS_CONTAINER_PATH,
            "PYNCHY_IPC_DIR": PYNCHY_IPC_CONTAINER_PATH,
        }
    )
    if input_data.automation_memory_dir is not None:
        environment["PYNCHY_AUTOMATION_MEMORY_DIR"] = AGENT_AUTOMATION_MEMORY_CONTAINER_PATH
    return environment


# ---------------------------------------------------------------------------
# Shared spawn logic
# ---------------------------------------------------------------------------


async def _spawn_container(
    group: WorkspaceProfile,
    input_data: ContainerInput,
    container_name: str,
    runtime: AgentExecutionRuntime,
    plugin_manager: pluggy.PluginManager | None = None,
) -> tuple[asyncio.subprocess.Process, tuple[McpStartupFailure, ...]]:
    """Resolve environment, build mounts, and spawn a container subprocess.

    Returns the process and optional MCP startup failures to the session owner.

    Raises OSError if the subprocess fails to start.
    """
    start_time = time.monotonic()

    # Create session-scoped SecurityGate keyed by (group_folder, invocation_ts).
    # Must exist before the container starts so IPC/MCP handlers can look it up.
    from pynchy.host.container_manager.security.gate import (  # noqa: PLC0415 - security gate setup is only needed during container spawn.
        create_gate,
        resolve_security,
    )

    security = resolve_security(group.folder, is_admin=input_data.is_admin)
    invocation_ts = start_time
    create_gate(
        group.folder,
        invocation_ts,
        security,
        public_source_input=input_data.corruption_tainted,
        secret_source_input=input_data.secret_tainted,
    )
    input_data.invocation_ts = invocation_ts

    # The harness deliberately defers this expensive check at host startup,
    # but an agent container must never be spawned without its image.
    container_cli, ensure_agent_image, resolve_repo_mounts = _configured_container_runtime()
    await asyncio.to_thread(
        ensure_agent_image,
        project_root=runtime.project_root,
        image=runtime.agent_image,
    )
    group_dir = runtime.groups_dir / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    # --- Resolve worktree ---
    phase_start = time.monotonic()
    repo_mounts: list[RepoMount] = []
    if input_data.repo_accesses:
        # Preflight resolves workspace defaults or an invocation-specific override
        # into this semantic mount scope. Re-reading the workspace here would widen
        # an explicitly scoped scheduled task back to every configured repository.
        resolution = resolve_repo_mounts(group.folder, tuple(input_data.repo_accesses))
        repo_mounts.extend(resolution.mounts)
        if resolution.notices:
            if input_data.system_notices is None:
                input_data.system_notices = []
            input_data.system_notices.extend(resolution.notices)
    worktree_ms = (time.monotonic() - phase_start) * 1000

    # --- Build mounts ---
    phase_start = time.monotonic()
    agent_hooks = collect_agent_hook_specs(plugin_manager)
    input_data.plugin_hooks = container_agent_hook_configs(agent_hooks)
    mounts = build_volume_mounts(
        group,
        is_admin=input_data.is_admin,
        groups_dir=runtime.groups_dir,
        data_dir=runtime.data_dir,
        project_root=runtime.project_root,
        mount_allowlist_path=runtime.mount_allowlist_path,
        blocked_mount_patterns=runtime.blocked_mount_patterns,
        plugin_manager=plugin_manager,
        repo_mounts=repo_mounts,
        agent_hooks=agent_hooks,
        automation_memory_dir=(
            Path(input_data.automation_memory_dir)
            if input_data.automation_memory_dir is not None
            else None
        ),
    )
    mounts_ms = (time.monotonic() - phase_start) * 1000

    # --- MCP gateway: ensure containers running and pass credentials ---
    phase_start = time.monotonic()
    from pynchy.host.container_manager.mcp.manager import (  # noqa: PLC0415 - MCP manager is only needed during container spawn.
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

    agent_env = _container_agent_environment(group, input_data)

    # --- Build args ---
    container_args = build_container_args(
        mounts,
        container_name,
        memory_mb=runtime.agent_memory_mb,
        image=runtime.agent_image,
        env_names=tuple(agent_env),
    )

    # --- Write initial input as file (container reads on startup) ---
    ipc_input_dir = runtime.data_dir / "ipc" / group.folder / "input"
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
        container_cli,
        *container_args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=filtered_process_environment(agent_env),
    )

    return proc, mcp_startup_failures
