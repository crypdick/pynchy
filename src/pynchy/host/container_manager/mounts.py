"""Volume mount list construction and container CLI arg building."""

from __future__ import annotations

from collections.abc import (
    Callable,
)
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pluggy

    from pynchy.plugins.api import AgentHookSpec

from pynchy.agent_protocol.api import VolumeMount
from pynchy.host.container_manager.contracts import AgentHomeMounts, RepoMount
from pynchy.host.container_manager.security.mount_security import validate_additional_mounts
from pynchy.host.paths import (
    AGENT_AUTOMATION_MEMORY_CONTAINER_PATH,
    AGENT_WORKSPACE_CONTAINER_PATH,
    PERSONALIZATION_RELATIVE_DIR,
    PERSONALIZATION_SKILLS_CONTAINER_PATH,
    PYNCHY_AGENT_RUNNER_CONTAINER_PATH,
    PYNCHY_IPC_CONTAINER_PATH,
    PYNCHY_SCRIPTS_CONTAINER_PATH,
    PYNCHY_SECRETS_CONTAINER_PATH,
    SKILLS_DIRNAME,
)
from pynchy.plugins.api import agent_hook_mounts
from pynchy.workspace.api import (
    WorkspaceProfile,
)


@dataclass(frozen=True, slots=True)
class MountOperations:
    """Concrete host operations needed to assemble container mounts."""

    prepare_agent_homes: Callable[..., AgentHomeMounts]
    repo_container_path: Callable[[str], str]
    runtime_name: Callable[[], str]


_mount_operations: MountOperations | None = None


def configure_mount_operations(operations: MountOperations) -> None:
    """Install composition-owned mount operations for this host process."""
    global _mount_operations  # noqa: PLW0603 - one host process owns mount composition.
    _mount_operations = operations


def _configured_mount_operations() -> MountOperations:
    if _mount_operations is None:
        raise RuntimeError("container mount operations have not been configured")
    return _mount_operations


def build_volume_mounts(  # noqa: PLR0913 - orchestration entry point with explicit mount inputs.
    group: WorkspaceProfile,
    *,
    is_admin: bool,
    groups_dir: Path,
    data_dir: Path,
    project_root: Path,
    mount_allowlist_path: Path,
    blocked_mount_patterns: tuple[str, ...],
    plugin_manager: pluggy.PluginManager | None = None,
    repo_ctx: RepoMount | None = None,
    worktree_path: Path | None = None,
    repo_mounts: list[RepoMount] | None = None,
    agent_hooks: tuple[AgentHookSpec, ...] = (),
    automation_memory_dir: Path | None = None,
) -> list[VolumeMount]:
    """Build the mount list for a container invocation.

    Args:
        group: The registered group configuration
        is_admin: Whether this is the admin group
        plugin_manager: Optional pluggy.PluginManager for plugin MCP mounts
        repo_ctx: Single resolved repo, paired with ``worktree_path`` when provided
        worktree_path: Single worktree path, paired with ``repo_ctx`` when provided
        repo_mounts: Resolved repo/worktree pairs for every configured repo
        agent_hooks: Trusted lifecycle hook modules to mount read-only

    Returns:
        List of volume mounts for the container
    """
    mounts: list[VolumeMount] = []
    operations = _configured_mount_operations()

    group_dir = groups_dir / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)
    effective_repo_mounts = _effective_repo_mounts(repo_ctx, worktree_path, repo_mounts)

    agent_homes = operations.prepare_agent_homes(group.folder, plugin_manager)
    if agent_homes.vault_mount_root is not None and agent_homes.vault_mount_path is not None:
        mounts.append(
            VolumeMount(
                str(agent_homes.vault_mount_root),
                str(agent_homes.vault_mount_path),
                readonly=False,
            )
        )
    if automation_memory_dir is not None:
        mounts.append(
            VolumeMount(
                str(automation_memory_dir),
                AGENT_AUTOMATION_MEMORY_CONTAINER_PATH,
                readonly=False,
            )
        )
    personalization_skills = project_root / PERSONALIZATION_RELATIVE_DIR / SKILLS_DIRNAME
    personalization_skills.mkdir(parents=True, exist_ok=True)
    mounts.append(
        VolumeMount(
            str(personalization_skills),
            PERSONALIZATION_SKILLS_CONTAINER_PATH,
            readonly=False,
        )
    )
    _add_workspace_mounts(mounts, group_dir, effective_repo_mounts, operations)
    mounts.append(VolumeMount(str(agent_homes.claude_home), "/home/agent/.claude", readonly=False))
    mounts.append(VolumeMount(str(agent_homes.codex_home), "/home/agent/.codex", readonly=False))
    mounts.extend(agent_hook_mounts(agent_hooks))

    _add_ipc_mount(mounts, data_dir, group.folder)

    # Guard scripts (read-only: hook script + settings overlay)
    scripts_dir = project_root / "src" / "pynchy" / "agent" / "scripts"
    if scripts_dir.exists():
        mounts.append(VolumeMount(str(scripts_dir), PYNCHY_SCRIPTS_CONTAINER_PATH, readonly=True))

    # Agent-runner source (read-only, Python source for container)
    agent_runner_src = project_root / "src" / "pynchy" / "agent" / "agent_runner" / "src"
    mounts.append(
        VolumeMount(str(agent_runner_src), PYNCHY_AGENT_RUNNER_CONTAINER_PATH, readonly=True)
    )

    _add_raw_repo_mount(
        mounts,
        effective_repo_mounts,
        is_admin=is_admin,
    )

    _add_validated_additional_mounts(
        mounts,
        group,
        is_admin=is_admin,
        allowlist_path=mount_allowlist_path,
        blocked_patterns=blocked_mount_patterns,
    )

    return mounts


def _effective_repo_mounts(
    repo_ctx: RepoMount | None,
    worktree_path: Path | None,
    repo_mounts: list[RepoMount] | None,
) -> list[RepoMount]:
    if repo_mounts is not None:
        return repo_mounts
    if repo_ctx is not None and worktree_path is not None:
        return [
            RepoMount(
                slug=repo_ctx.slug,
                root=repo_ctx.root,
                worktree_path=worktree_path,
            )
        ]
    return []


def _add_workspace_mounts(
    mounts: list[VolumeMount],
    group_dir: Path,
    repo_mounts: list[RepoMount],
    operations: MountOperations,
) -> None:
    for repo_mount in repo_mounts:
        mounts.append(
            VolumeMount(
                str(repo_mount.worktree_path),
                operations.repo_container_path(repo_mount.slug),
                readonly=False,
            )
        )
        # Worktree .git file references the main repo's .git dir via absolute path.
        # Mount it at the same host path so git resolves the reference inside the container.
        git_dir = repo_mount.root / ".git"
        mounts.append(VolumeMount(str(git_dir), str(git_dir), readonly=False))
    mounts.append(VolumeMount(str(group_dir), AGENT_WORKSPACE_CONTAINER_PATH, readonly=False))


def _add_ipc_mount(mounts: list[VolumeMount], data_dir: Path, group_folder: str) -> None:
    group_ipc_dir = data_dir / "ipc" / group_folder
    # The host writes responses to security and service requests. Create the
    # directory as the host so its ownership remains valid even for a
    # root-running deterministic runtime container.
    for sub in (
        "messages",
        "requests",
        "responses",
        "input",
        "output",
        "merge_results",
        "secrets",
    ):
        (group_ipc_dir / sub).mkdir(parents=True, exist_ok=True)
    mounts.append(VolumeMount(str(group_ipc_dir), PYNCHY_IPC_CONTAINER_PATH, readonly=False))
    mounts.append(
        VolumeMount(
            str(group_ipc_dir / "secrets"),
            PYNCHY_SECRETS_CONTAINER_PATH,
            readonly=False,
        )
    )


def _add_raw_repo_mount(
    mounts: list[VolumeMount],
    repo_mounts: list[RepoMount],
    *,
    is_admin: bool,
) -> None:
    # Admin groups get a read-write mount of the actual host repo root.
    # This gives them direct access to personalization, data/, other worktrees, etc.
    # without going through the git sync workflow.  The path is intentionally
    # alarming so agents default to their worktree for normal work.
    if not is_admin:
        return
    mounts.extend(
        [
            VolumeMount(
                str(repo_mount.root),
                f"/danger/raw-host-repos/{repo_mount.slug}",
                readonly=False,
            )
            for repo_mount in repo_mounts
        ]
    )


def _add_validated_additional_mounts(
    mounts: list[VolumeMount],
    group: WorkspaceProfile,
    *,
    is_admin: bool,
    allowlist_path: Path,
    blocked_patterns: tuple[str, ...],
) -> None:
    if group.container_config is None or not group.container_config.additional_mounts:
        return

    validated = validate_additional_mounts(
        group.container_config.additional_mounts,
        group.name,
        is_admin=is_admin,
        allowlist_path=allowlist_path,
        default_blocked_patterns=blocked_patterns,
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


def build_container_args(
    mounts: list[VolumeMount],
    container_name: str,
    *,
    memory_mb: int,
    image: str,
    env_names: tuple[str, ...] = (),
) -> list[str]:
    """Build CLI args for `container run`."""
    from pynchy.container_labels import (  # noqa: PLC0415 - labels are only needed when building container argv.
        AGENT_CONTAINER_LABEL,
        AGENT_CONTAINER_LABEL_VALUE,
    )
    from pynchy.host.container_manager.gateway import (  # noqa: PLC0415 - keep gateway lookup lazy and patchable for container arg tests.
        get_gateway,
    )

    runtime_name = _configured_mount_operations().runtime_name()

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

    args.extend(["--memory", f"{memory_mb}m"])

    # When the gateway is active and we're using Docker, add a host mapping
    # so containers can reach the host process via ``host.docker.internal``.
    # Docker Desktop sets this automatically; on Linux it requires --add-host.
    gateway = get_gateway()
    if gateway is not None and runtime_name == "docker":
        args.extend(["--add-host", "host.docker.internal:host-gateway"])

    for name in sorted(env_names):
        args.extend(["-e", name])

    for m in mounts:
        if m.readonly:
            # Apple Container rejects file sources with --mount ...,readonly,
            # but accepts the same file bind through -v ...:ro.
            if runtime_name == "apple" and Path(m.host_path).is_file():
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
    args.append(image)
    return args
