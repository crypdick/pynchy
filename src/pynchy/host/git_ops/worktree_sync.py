"""Publication preconditions for ordinary isolated worktrees."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pynchy.host.git_ops.repo import (
    RepoContext,  # noqa: TC001 - beartype resolves worktree sync signatures at runtime.
)
from pynchy.host.git_ops.utils import (
    count_commits,
    detect_main_branch,
    git_env_with_token,
    run_git,
)
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)


@runtime_checkable
class GitSyncDeps(Protocol):
    """Dependencies for the git sync loop."""

    async def broadcast_host_message(self, jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, jid: str, text: str) -> None: ...

    async def wake_worktree_conflict(self, jid: str) -> None: ...

    def has_active_session(self, group_folder: str) -> bool: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None: ...


@dataclasses.dataclass(frozen=True)
class _WorktreeContext:
    """Validated context for worktree synchronization and PR publication."""

    worktree_path: Path
    branch_name: str
    main_branch: str
    env: dict[str, str] | None
    ahead: int
    log_group: str
    base_sha: str | None = None
    head_sha: str | None = None
    object_dir: Path | None = None
    object_format: str | None = None
    remote_url: str | None = None


def _validate_sync_preconditions(  # noqa: PLR0911 - fail-closed preconditions need distinct diagnostics.
    group_folder: str,
    repo_ctx: RepoContext,
) -> _WorktreeContext | dict[str, Any]:
    """Validate an ordinary isolated worktree before PR publication."""
    worktree_path = repo_ctx.worktrees_dir / group_folder
    env = git_env_with_token(repo_ctx.slug)

    if not worktree_path.exists():
        return {
            "success": False,
            "message": f"No worktree found for {group_folder}. Nothing to sync.",
        }

    try:
        resolved_worktree = worktree_path.resolve()
        resolved_root = repo_ctx.root.resolve()
    except OSError:
        return {"success": False, "message": "Could not resolve the isolated worktree path."}
    if resolved_worktree == resolved_root or not _is_registered_worktree(
        resolved_worktree, repo_ctx
    ):
        return {
            "success": False,
            "message": (
                "Publication blocked: isolated worktree is not registered with its repository."
            ),
        }

    branch = run_git("branch", "--show-current", cwd=resolved_worktree)
    branch_name = branch.stdout.strip()
    if branch.returncode != 0 or not branch_name:
        return {
            "success": False,
            "message": "Publication blocked: isolated worktree is detached or has no branch.",
        }

    status = run_git("status", "--porcelain", cwd=resolved_worktree)
    if status.returncode == 0 and status.stdout.strip():
        return {
            "success": False,
            "message": (
                "You have uncommitted changes. Commit all changes first, "
                "then call sync_worktree_to_main again.\n"
                "Run `git status` to see uncommitted files."
            ),
        }

    main_branch = detect_main_branch(cwd=repo_ctx.root)
    fetch = run_git("fetch", "origin", cwd=repo_ctx.root, env=env)
    if fetch.returncode != 0:
        return {
            "success": False,
            "message": "git fetch failed. Check repository access and try again.",
        }
    ahead = count_commits(f"origin/{main_branch}..HEAD", cwd=resolved_worktree)
    if ahead is None:
        return {
            "success": False,
            "message": (
                "Failed to check commits on your branch. "
                "Verify your branch is valid with `git log --oneline`."
            ),
        }
    if ahead == 0:
        return {
            "success": True,
            "message": "Already up to date — no new commits.",
        }

    return _WorktreeContext(
        worktree_path=resolved_worktree,
        branch_name=branch_name,
        main_branch=main_branch,
        env=env,
        ahead=ahead,
        log_group=group_folder,
    )


def _is_registered_worktree(worktree_path: Path, repo_ctx: RepoContext) -> bool:
    """Require publication path to be an exact Git-registered child worktree."""
    registered = run_git("worktree", "list", "--porcelain", cwd=repo_ctx.root)
    if registered.returncode != 0:
        return False
    try:
        paths = {
            Path(line.removeprefix("worktree ")).resolve()
            for line in registered.stdout.splitlines()
            if line.startswith("worktree ")
        }
    except OSError:
        return False
    return worktree_path in paths
