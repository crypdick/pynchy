"""Volume mount list construction and container CLI arg building."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pluggy

from pynchy.config import get_settings
from pynchy.container_runner._credentials import _write_env_file
from pynchy.container_runner._session_prep import _sync_skills, _write_settings_json
from pynchy.logger import logger
from pynchy.security.mount_security import validate_additional_mounts
from pynchy.types import RegisteredGroup, VolumeMount
from pynchy.workspace_config import load_workspace_config


def _build_volume_mounts(
    group: RegisteredGroup,
    is_god: bool,
    plugin_manager: pluggy.PluginManager | None = None,
    project_access: bool = False,
    worktree_path: Path | None = None,
) -> list[VolumeMount]:
    """Build the mount list for a container invocation.

    Args:
        group: The registered group configuration
        is_god: Whether this is the god group
        plugin_manager: Optional pluggy.PluginManager for plugin MCP mounts
        project_access: Whether to mount the host project into the container
        worktree_path: Pre-resolved worktree path for non-main project_access groups

    Returns:
        List of volume mounts for the container
    """
    s = get_settings()
    mounts: list[VolumeMount] = []

    group_dir = s.groups_dir / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    if worktree_path:
        mounts.append(VolumeMount(str(worktree_path), "/workspace/project", readonly=False))
        # Worktree .git file references the main repo's .git dir via absolute path.
        # Mount it at the same host path so git resolves the reference inside the container.
        git_dir = s.project_root / ".git"
        mounts.append(VolumeMount(str(git_dir), str(git_dir), readonly=False))
        mounts.append(VolumeMount(str(group_dir), "/workspace/group", readonly=False))
    else:
        mounts.append(VolumeMount(str(group_dir), "/workspace/group", readonly=False))
        global_dir = s.groups_dir / "global"
        if global_dir.exists():
            mounts.append(VolumeMount(str(global_dir), "/workspace/global", readonly=True))

    # Per-group Claude sessions directory (isolated from other groups)
    session_dir = s.data_dir / "sessions" / group.folder / ".claude"
    session_dir.mkdir(parents=True, exist_ok=True)
    _write_settings_json(session_dir)
    ws_config = load_workspace_config(group.folder)
    _sync_skills(
        session_dir,
        plugin_manager,
        workspace_skills=ws_config.skills if ws_config else None,
    )
    mounts.append(VolumeMount(str(session_dir), "/home/agent/.claude", readonly=False))

    # Per-group IPC namespace
    group_ipc_dir = s.data_dir / "ipc" / group.folder
    for sub in ("messages", "tasks", "input", "merge_results"):
        (group_ipc_dir / sub).mkdir(parents=True, exist_ok=True)
    mounts.append(VolumeMount(str(group_ipc_dir), "/workspace/ipc", readonly=False))

    # Guard scripts (read-only: hook script + settings overlay)
    scripts_dir = s.project_root / "container" / "scripts"
    if scripts_dir.exists():
        mounts.append(VolumeMount(str(scripts_dir), "/workspace/scripts", readonly=True))

    # Environment file directory (per-group, GH_TOKEN scoped to god only)
    env_dir = _write_env_file(is_god=is_god, group_folder=group.folder)
    if env_dir is not None:
        mounts.append(VolumeMount(str(env_dir), "/workspace/env-dir", readonly=True))

    # Agent-runner source (read-only, Python source for container)
    agent_runner_src = s.project_root / "container" / "agent_runner" / "src"
    mounts.append(VolumeMount(str(agent_runner_src), "/app/src", readonly=True))

    # Host config.toml — only the god (admin) container gets access to this.
    # config.toml is .gitignored so it's absent from worktrees; mounting it
    # directly lets the admin agent edit settings without host-side access.
    if is_god:
        config_toml = s.project_root / "config.toml"
        if config_toml.exists():
            mounts.append(
                VolumeMount(str(config_toml), "/workspace/project/config.toml", readonly=False)
            )

    # Additional mounts validated against external allowlist
    if group.container_config and group.container_config.additional_mounts:
        validated = validate_additional_mounts(
            group.container_config.additional_mounts, group.name, is_god
        )
        for m in validated:
            mounts.append(
                VolumeMount(
                    host_path=str(m["hostPath"]),
                    container_path=str(m["containerPath"]),
                    readonly=bool(m["readonly"]),
                )
            )

    # Plugin MCP server source mounts
    if plugin_manager:
        mcp_specs_list = plugin_manager.hook.pynchy_mcp_server_spec()
        for spec in mcp_specs_list:
            try:
                if spec.get("host_source"):
                    # Mount plugin source to /workspace/plugins/{name}/
                    mounts.append(
                        VolumeMount(
                            host_path=str(spec["host_source"]),
                            container_path=f"/workspace/plugins/{spec['name']}",
                            readonly=True,
                        )
                    )
            except Exception:
                logger.exception(
                    "Failed to mount plugin MCP source",
                    plugin_name=spec.get("name", "unknown"),
                    host_source=str(spec.get("host_source", "")),
                )

    return mounts


def _build_container_args(mounts: list[VolumeMount], container_name: str) -> list[str]:
    """Build CLI args for `container run`."""
    from pynchy.container_runner.gateway import get_gateway
    from pynchy.runtime.runtime import get_runtime

    args = ["run", "-i", "--rm", "--name", container_name]

    # When the gateway is active and we're using Docker, add a host mapping
    # so containers can reach the host process via ``host.docker.internal``.
    # Docker Desktop sets this automatically; on Linux it requires --add-host.
    gateway = get_gateway()
    if gateway is not None and get_runtime().name == "docker":
        args.extend(["--add-host", "host.docker.internal:host-gateway"])

    for m in mounts:
        if m.readonly:
            args.extend(
                [
                    "--mount",
                    f"type=bind,source={m.host_path},target={m.container_path},readonly",
                ]
            )
        else:
            args.extend(["-v", f"{m.host_path}:{m.container_path}"])
    args.append(get_settings().container.image)
    return args
