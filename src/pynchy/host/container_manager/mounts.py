"""Volume mount list construction and container CLI arg building."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pluggy

from pynchy.config import get_settings
from pynchy.host.container_manager.credentials import write_env_file
from pynchy.host.container_manager.onecli import prepare_onecli_material
from pynchy.host.container_manager.security.mount_security import validate_additional_mounts
from pynchy.host.container_manager.session_prep import sync_skills, write_settings_json
from pynchy.host.git_ops.repo import RepoContext, repo_container_path
from pynchy.host.learning.mirror import prepare_vault_mount_root
from pynchy.host.learning.paths import LearningConfigError, resolve_learning_paths
from pynchy.host.learning.skills import iter_learned_skill_dirs
from pynchy.host.orchestrator.workspace_config import load_resolved_config
from pynchy.types import VolumeMount, WorkspaceProfile

_LEARNING_VAULT_DIRECTORY_REQUIRED_ERROR = (
    "learning.obsidian.vault_root must be an existing directory"
)
_LEARNING_REVIEW_FOLDER_PREFIX = "learning-review-"


@dataclass(frozen=True)
class _LearningMountContext:
    workspace_skills: list[str] | None
    denied_skill_names: list[str] | None
    learned_skill_paths: list[Path] | None


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


def build_volume_mounts(  # noqa: PLR0913, RUF100 - orchestration entry point with explicit mount inputs.
    group: WorkspaceProfile,
    *,
    is_admin: bool,
    plugin_manager: pluggy.PluginManager | None = None,
    repo_ctx: RepoContext | None = None,
    worktree_path: Path | None = None,
    repo_mounts: list[tuple[RepoContext, Path]] | None = None,
) -> list[VolumeMount]:
    """Build the mount list for a container invocation.

    Args:
        group: The registered group configuration
        is_admin: Whether this is the admin group
        plugin_manager: Optional pluggy.PluginManager for plugin MCP mounts
        repo_ctx: Single resolved repo, paired with ``worktree_path`` when provided
        worktree_path: Single worktree path, paired with ``repo_ctx`` when provided
        repo_mounts: Resolved repo/worktree pairs for every configured repo

    Returns:
        List of volume mounts for the container
    """
    s = get_settings()
    mounts: list[VolumeMount] = []

    group_dir = s.groups_dir / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)
    effective_repo_mounts = _effective_repo_mounts(repo_ctx, worktree_path, repo_mounts)

    learning = _add_learning_mounts(mounts, group.folder)
    _add_workspace_mounts(mounts, group_dir, effective_repo_mounts)
    _add_claude_session_mount(
        mounts,
        data_dir=s.data_dir,
        group_folder=group.folder,
        plugin_manager=plugin_manager,
        learning=learning,
    )

    codex_home = _prepare_codex_home(group.folder)
    sync_skills(
        codex_home,
        plugin_manager,
        workspace_skills=learning.workspace_skills,
        denied_skill_names=learning.denied_skill_names,
        learned_skill_paths=learning.learned_skill_paths,
    )
    mounts.append(VolumeMount(str(codex_home), "/home/agent/.codex", readonly=False))

    _add_ipc_mount(mounts, s.data_dir, group.folder)

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
    env_dir = write_env_file(
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

    _add_raw_repo_mount(
        mounts,
        [repo_ctx for repo_ctx, _ in effective_repo_mounts],
        is_admin=is_admin,
    )

    _add_validated_additional_mounts(mounts, group, is_admin=is_admin)

    return mounts


def _effective_repo_mounts(
    repo_ctx: RepoContext | None,
    worktree_path: Path | None,
    repo_mounts: list[tuple[RepoContext, Path]] | None,
) -> list[tuple[RepoContext, Path]]:
    if repo_mounts is not None:
        return repo_mounts
    if repo_ctx is not None and worktree_path is not None:
        return [(repo_ctx, worktree_path)]
    return []


def _add_learning_mounts(mounts: list[VolumeMount], group_folder: str) -> _LearningMountContext:
    resolved = load_resolved_config(group_folder)
    workspace_skills = resolved.skills if resolved else None
    denied_skill_names = resolved.denied_skills if resolved else None
    learning_paths = resolve_learning_paths(
        group_folder,
        profile_override=_learning_profile_override_for_group(group_folder),
    )
    learned_skill_paths: list[Path] | None = None
    if learning_paths is None:
        return _LearningMountContext(
            workspace_skills=workspace_skills,
            denied_skill_names=denied_skill_names,
            learned_skill_paths=learned_skill_paths,
        )

    workspace_skills = _with_learned_skill_tier(workspace_skills)
    _validate_learning_vault(learning_paths.vault_root)
    learning_paths.memory_root.mkdir(parents=True, exist_ok=True)
    learning_paths.skills_root.mkdir(parents=True, exist_ok=True)
    mounts.append(
        VolumeMount(
            str(prepare_vault_mount_root(learning_paths)),
            learning_paths.vault_mount_path,
            readonly=False,
        )
    )
    learned_skill_paths = iter_learned_skill_dirs(group_folder)
    return _LearningMountContext(
        workspace_skills=workspace_skills,
        denied_skill_names=denied_skill_names,
        learned_skill_paths=learned_skill_paths,
    )


def _validate_learning_vault(vault_root: Path) -> None:
    if vault_root.exists() and vault_root.is_dir():
        return
    raise LearningConfigError(_LEARNING_VAULT_DIRECTORY_REQUIRED_ERROR)


def _with_learned_skill_tier(workspace_skills: list[str] | None) -> list[str]:
    """Include reviewer-created skills whenever Obsidian learning is enabled."""
    # NOTE: Keep docs/usage/memory.md and docs/architecture/memory-and-sessions.md
    # aligned with this activation contract.
    if workspace_skills is None:
        return ["learned"]
    if "*" in workspace_skills or "learned" in workspace_skills:
        return workspace_skills
    return [*workspace_skills, "learned"]


def _add_workspace_mounts(
    mounts: list[VolumeMount],
    group_dir: Path,
    repo_mounts: list[tuple[RepoContext, Path]],
) -> None:
    for repo_ctx, worktree_path in repo_mounts:
        mounts.append(
            VolumeMount(str(worktree_path), repo_container_path(repo_ctx.slug), readonly=False)
        )
        # Worktree .git file references the main repo's .git dir via absolute path.
        # Mount it at the same host path so git resolves the reference inside the container.
        git_dir = repo_ctx.root / ".git"
        mounts.append(VolumeMount(str(git_dir), str(git_dir), readonly=False))
    mounts.append(VolumeMount(str(group_dir), "/workspace/group", readonly=False))


def _add_claude_session_mount(
    mounts: list[VolumeMount],
    *,
    data_dir: Path,
    group_folder: str,
    plugin_manager: pluggy.PluginManager | None,
    learning: _LearningMountContext,
) -> None:
    session_dir = data_dir / "sessions" / group_folder / ".claude"
    session_dir.mkdir(parents=True, exist_ok=True)
    write_settings_json(session_dir)
    sync_skills(
        session_dir,
        plugin_manager,
        workspace_skills=learning.workspace_skills,
        denied_skill_names=learning.denied_skill_names,
        learned_skill_paths=learning.learned_skill_paths,
    )
    mounts.append(VolumeMount(str(session_dir), "/home/agent/.claude", readonly=False))


def _add_ipc_mount(mounts: list[VolumeMount], data_dir: Path, group_folder: str) -> None:
    group_ipc_dir = data_dir / "ipc" / group_folder
    # The host writes responses to security and service requests. Create the
    # directory as the host so its ownership remains valid even for a
    # root-running deterministic runtime container.
    for sub in ("messages", "requests", "responses", "input", "output", "merge_results"):
        (group_ipc_dir / sub).mkdir(parents=True, exist_ok=True)
    mounts.append(VolumeMount(str(group_ipc_dir), "/workspace/ipc", readonly=False))


def _add_raw_repo_mount(
    mounts: list[VolumeMount],
    repo_contexts: list[RepoContext],
    *,
    is_admin: bool,
) -> None:
    # Admin groups get a read-write mount of the actual host repo root.
    # This gives them direct access to config.toml, data/, other worktrees, etc.
    # without going through the git sync workflow.  The path is intentionally
    # alarming so agents default to their worktree for normal work.
    if not is_admin:
        return
    mounts.extend(
        [
            VolumeMount(
                str(repo_ctx.root),
                f"/danger/raw-host-repos/{repo_ctx.slug}",
                readonly=False,
            )
            for repo_ctx in repo_contexts
        ]
    )


def _add_validated_additional_mounts(
    mounts: list[VolumeMount],
    group: WorkspaceProfile,
    *,
    is_admin: bool,
) -> None:
    if group.container_config is None or not group.container_config.additional_mounts:
        return

    validated = validate_additional_mounts(
        group.container_config.additional_mounts,
        group.name,
        is_admin=is_admin,
    )
    mounts.extend(
        [
            VolumeMount(
                host_path=str(mount["hostPath"]),
                container_path=str(mount["containerPath"]),
                readonly=bool(mount["readonly"]),
            )
            for mount in validated
        ]
    )


def _learning_profile_override_for_group(group_folder: str) -> str | None:
    if not group_folder.startswith(_LEARNING_REVIEW_FOLDER_PREFIX):
        return None
    profile_slug = group_folder.removeprefix(_LEARNING_REVIEW_FOLDER_PREFIX)
    return profile_slug or None


def build_container_args(mounts: list[VolumeMount], container_name: str) -> list[str]:
    """Build CLI args for `container run`."""
    from pynchy.host.container_manager.gateway import (  # noqa: PLC0415, RUF100 - keep gateway lookup lazy and patchable for container arg tests.
        get_gateway,
    )
    from pynchy.host.container_manager.labels import (  # noqa: PLC0415, RUF100 - labels are only needed when building container argv.
        AGENT_CONTAINER_LABEL,
        AGENT_CONTAINER_LABEL_VALUE,
    )
    from pynchy.plugins.runtimes.detection import (  # noqa: PLC0415, RUF100 - runtime detection is only needed when building container argv.
        get_runtime,
    )

    # No --rm: persistent sessions need explicit cleanup via docker rm -f
    # (handled in _session.py on stop/create).
    # No -i: stdin is DEVNULL (input arrives via IPC files).
    args = [
        "run",
        "--name",
        container_name,
        "--label",
        f"{AGENT_CONTAINER_LABEL}={AGENT_CONTAINER_LABEL_VALUE}",
    ]

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
