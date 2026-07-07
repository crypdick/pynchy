"""Volume mount list construction and container CLI arg building."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pluggy

from pynchy.config import get_settings
from pynchy.host.container_manager.credentials import _write_env_file
from pynchy.host.container_manager.onecli import prepare_onecli_material
from pynchy.host.container_manager.security.mount_security import validate_additional_mounts
from pynchy.host.container_manager.session_prep import _sync_skills, _write_settings_json
from pynchy.host.git_ops.repo import RepoContext
from pynchy.host.learning.paths import LearningConfigError, resolve_learning_paths
from pynchy.host.learning.skills import iter_learned_skill_dirs
from pynchy.host.orchestrator.workspace_config import load_resolved_config
from pynchy.types import VolumeMount, WorkspaceProfile


def _prepare_codex_home(group_folder: str) -> Path:
    """Create per-group Codex CLI state.

    Codex still needs a writable home for generated config, sessions, and
    history, but model authentication comes from the Pynchy gateway env vars.
    Do not copy host ``~/.codex/auth.json`` into sandboxes.
    """
    s = get_settings()
    codex_home = s.data_dir / "sessions" / group_folder / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    return codex_home


def build_volume_mounts(
    group: WorkspaceProfile,
    is_admin: bool,
    plugin_manager: pluggy.PluginManager | None = None,
    repo_ctx: RepoContext | None = None,
    worktree_path: Path | None = None,
) -> list[VolumeMount]:
    """Build the mount list for a container invocation.

    Args:
        group: The registered group configuration
        is_admin: Whether this is the admin group
        plugin_manager: Optional pluggy.PluginManager for plugin MCP mounts
        repo_ctx: Resolved repo context when group has repo_access; None otherwise
        worktree_path: Pre-resolved worktree path for repo_access groups

    Returns:
        List of volume mounts for the container
    """
    s = get_settings()
    mounts: list[VolumeMount] = []

    group_dir = s.groups_dir / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    resolved = load_resolved_config(group.folder)
    workspace_skills = resolved.skills if resolved else None
    learning_paths = resolve_learning_paths(group.folder)
    learned_skill_paths: list[Path] | None = None
    if learning_paths is not None:
        if not learning_paths.vault_root.exists() or not learning_paths.vault_root.is_dir():
            raise LearningConfigError("learning.obsidian.vault_root must be an existing directory")
        learning_paths.memory_root.mkdir(parents=True, exist_ok=True)
        learning_paths.skills_root.mkdir(parents=True, exist_ok=True)
        mounts.append(
            VolumeMount(
                str(learning_paths.vault_root),
                learning_paths.vault_mount_path,
                readonly=False,
            )
        )
        if workspace_skills is not None:
            learned_skill_paths = iter_learned_skill_dirs(group.folder)

    if worktree_path and repo_ctx:
        mounts.append(VolumeMount(str(worktree_path), "/workspace/project", readonly=False))
        # Worktree .git file references the main repo's .git dir via absolute path.
        # Mount it at the same host path so git resolves the reference inside the container.
        git_dir = repo_ctx.root / ".git"
        mounts.append(VolumeMount(str(git_dir), str(git_dir), readonly=False))
        mounts.append(VolumeMount(str(group_dir), "/workspace/group", readonly=False))
    else:
        mounts.append(VolumeMount(str(group_dir), "/workspace/group", readonly=False))

    # Per-group Claude sessions directory (isolated from other groups)
    session_dir = s.data_dir / "sessions" / group.folder / ".claude"
    session_dir.mkdir(parents=True, exist_ok=True)
    _write_settings_json(session_dir)
    _sync_skills(
        session_dir,
        plugin_manager,
        workspace_skills=workspace_skills,
        learned_skill_paths=learned_skill_paths,
    )
    mounts.append(VolumeMount(str(session_dir), "/home/agent/.claude", readonly=False))

    codex_home = _prepare_codex_home(group.folder)
    mounts.append(VolumeMount(str(codex_home), "/home/agent/.codex", readonly=False))

    # Per-group IPC namespace
    group_ipc_dir = s.data_dir / "ipc" / group.folder
    for sub in ("messages", "requests", "input", "output", "merge_results"):
        (group_ipc_dir / sub).mkdir(parents=True, exist_ok=True)
    mounts.append(VolumeMount(str(group_ipc_dir), "/workspace/ipc", readonly=False))

    # Guard scripts (read-only: hook script + settings overlay)
    scripts_dir = s.project_root / "src" / "pynchy" / "agent" / "scripts"
    if scripts_dir.exists():
        mounts.append(VolumeMount(str(scripts_dir), "/workspace/scripts", readonly=True))

    # OneCLI material is proxy/CA/stub setup, not raw secrets.  When present,
    # OneCLI owns GitHub/API credential injection, so GH_TOKEN is not written.
    onecli_material = prepare_onecli_material(group.folder)
    if onecli_material is not None:
        mounts.extend(onecli_material.mounts)

    # Environment file directory (per-group, GH_TOKEN scoped to admin only)
    env_dir = _write_env_file(
        is_admin=is_admin,
        group_folder=group.folder,
        extra_env_vars=onecli_material.env_vars if onecli_material else None,
        include_gh_token=onecli_material is None,
    )
    if env_dir is not None:
        mounts.append(VolumeMount(str(env_dir), "/workspace/env-dir", readonly=True))

    # Agent-runner source (read-only, Python source for container)
    agent_runner_src = s.project_root / "src" / "pynchy" / "agent" / "agent_runner" / "src"
    mounts.append(VolumeMount(str(agent_runner_src), "/app/src", readonly=True))

    # Admin groups get a read-write mount of the actual host repo root.
    # This gives them direct access to config.toml, data/, other worktrees, etc.
    # without going through the git sync workflow.  The path is intentionally
    # alarming so agents default to their worktree for normal work.
    if is_admin and repo_ctx is not None:
        mounts.append(
            VolumeMount(
                str(repo_ctx.root),
                "/danger/raw-host-repo-mount-prefer-your-worktree",
                readonly=False,
            )
        )

    # Additional mounts validated against external allowlist
    if group.container_config and group.container_config.additional_mounts:
        validated = validate_additional_mounts(
            group.container_config.additional_mounts, group.name, is_admin
        )
        for m in validated:
            mounts.append(
                VolumeMount(
                    host_path=str(m["hostPath"]),
                    container_path=str(m["containerPath"]),
                    readonly=bool(m["readonly"]),
                )
            )

    return mounts


def build_container_args(mounts: list[VolumeMount], container_name: str) -> list[str]:
    """Build CLI args for `container run`."""
    from pynchy.host.container_manager.gateway import get_gateway
    from pynchy.plugins.runtimes.detection import get_runtime

    # No --rm: persistent sessions need explicit cleanup via docker rm -f
    # (handled in _session.py on stop/create).
    # No -i: stdin is DEVNULL (input arrives via IPC files).
    args = ["run", "--name", container_name]

    # When the gateway is active and we're using Docker, add a host mapping
    # so containers can reach the host process via ``host.docker.internal``.
    # Docker Desktop sets this automatically; on Linux it requires --add-host.
    gateway = get_gateway()
    runtime = get_runtime()
    if gateway is not None and runtime.name == "docker":
        args.extend(["--add-host", "host.docker.internal:host-gateway"])

    for m in mounts:
        if m.readonly:
            # Apple Container rejects file sources with --mount ...,readonly,
            # but accepts the same file bind through -v ...:ro.
            if runtime.name == "apple" and Path(m.host_path).is_file():
                args.extend(["-v", f"{m.host_path}:{m.container_path}:ro"])
            else:
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
