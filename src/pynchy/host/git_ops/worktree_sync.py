"""Merge and precondition operations for ordinary isolated worktrees."""

from __future__ import annotations

import dataclasses
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves protocol annotations at runtime.
)
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pynchy.host.git_ops.repo import (
    RepoContext,  # noqa: TC001 - beartype resolves worktree sync signatures at runtime.
)
from pynchy.host.git_ops.utils import (
    count_commits,
    count_unpushed_commits,
    detect_main_branch,
    git_env_with_token,
    push_local_commits,
    run_git,
)
from pynchy.logger import logger
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)

GIT_POLICY_MERGE = "merge-to-main"
GIT_POLICY_PR = "pull-request"


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
    *,
    compare_with_origin: bool = False,
) -> _WorktreeContext | dict[str, Any]:
    """Validate an ordinary isolated worktree before a sync or PR operation."""
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
    base_ref = main_branch
    if compare_with_origin:
        fetch = run_git("fetch", "origin", cwd=repo_ctx.root, env=env)
        if fetch.returncode != 0:
            return {
                "success": False,
                "message": "git fetch failed. Check repository access and try again.",
            }
        base_ref = f"origin/{main_branch}"
    ahead = count_commits(f"{base_ref}..HEAD", cwd=resolved_worktree)
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


def host_sync_worktree(group_folder: str, repo_ctx: RepoContext) -> dict[str, Any]:
    """Merge one isolated worktree into main and push it to origin."""
    ctx = _validate_sync_preconditions(group_folder, repo_ctx)
    if isinstance(ctx, dict):
        if ctx.get("success") and count_unpushed_commits(cwd=repo_ctx.root) > 0:
            return _retry_pending_main_push(repo_ctx)
        return ctx

    steps: tuple[Callable[[], dict[str, Any] | None], ...] = (
        lambda: _sync_fetch_origin(repo_ctx, ctx),
        lambda: _sync_rebase_main(repo_ctx, ctx),
        lambda: _sync_rebase_worktree(ctx),
        lambda: _sync_merge_worktree(repo_ctx, ctx),
        lambda: _sync_push_main(repo_ctx, ctx),
    )
    for step in steps:
        failure = step()
        if failure is not None:
            return failure

    logger.info(
        "Worktree synced to main and pushed",
        group=group_folder,
        commits=ctx.ahead,
    )
    return {
        "success": True,
        "message": f"Merged {ctx.ahead} commit(s) into main and pushed to origin.",
    }


def _retry_pending_main_push(repo_ctx: RepoContext) -> dict[str, Any]:
    pushed = push_local_commits(
        cwd=repo_ctx.root,
        env=git_env_with_token(repo_ctx.slug),
    )
    if pushed:
        return {
            "success": True,
            "message": "Published commits already merged into the host main branch.",
        }
    return {
        "success": False,
        "message": (
            "Push to origin still failed. Your commits remain on the host main branch; "
            "inspect the reported Git state and call sync_worktree_to_main again."
        ),
    }


def _sync_fetch_origin(repo_ctx: RepoContext, ctx: _WorktreeContext) -> dict[str, Any] | None:
    fetch = run_git("fetch", "origin", cwd=repo_ctx.root, env=ctx.env)
    if fetch.returncode != 0:
        return {
            "success": False,
            "message": (
                f"git fetch failed: {fetch.stderr.strip()}. "
                "Check network connectivity and try again."
            ),
        }
    return None


def _sync_rebase_main(repo_ctx: RepoContext, ctx: _WorktreeContext) -> dict[str, Any] | None:
    rebase_main = run_git("rebase", f"origin/{ctx.main_branch}", cwd=repo_ctx.root)
    if rebase_main.returncode != 0:
        run_git("rebase", "--abort", cwd=repo_ctx.root)
        return {
            "success": False,
            "message": (
                "Host main branch has conflicts with origin. "
                "This requires manual intervention on the host. "
                "Your worktree commits are preserved — try again later."
            ),
        }
    return None


def _sync_rebase_worktree(ctx: _WorktreeContext) -> dict[str, Any] | None:
    rebase_wt = run_git("rebase", ctx.main_branch, cwd=ctx.worktree_path)
    if rebase_wt.returncode != 0:
        return {
            "success": False,
            "message": (
                "Rebase conflict — your worktree has conflict markers. "
                "Fix them, then run:\n"
                "  git add <resolved files>\n"
                "  git rebase --continue\n"
                "Then call sync_worktree_to_main again."
            ),
        }
    return None


def _sync_merge_worktree(repo_ctx: RepoContext, ctx: _WorktreeContext) -> dict[str, Any] | None:
    merge = run_git("merge", "--ff-only", ctx.branch_name, cwd=repo_ctx.root)
    if merge.returncode != 0:
        return {
            "success": False,
            "message": (
                f"Fast-forward merge failed: {merge.stderr.strip()}. "
                "This is unexpected after a successful rebase. "
                "Try running `git log --oneline --graph` to inspect the state."
            ),
        }
    return None


def _sync_push_main(repo_ctx: RepoContext, ctx: _WorktreeContext) -> dict[str, Any] | None:
    pushed = push_local_commits(skip_fetch=True, cwd=repo_ctx.root, env=ctx.env)
    if not pushed:
        return {
            "success": False,
            "message": (
                "Merge succeeded but push to origin failed. "
                "Your commits are on the host's main branch. "
                "Call sync_worktree_to_main again to retry publication."
            ),
        }
    return None


def resolve_git_policy(_group_folder: str) -> str:
    """Return the one supported workspace Git policy."""
    return GIT_POLICY_MERGE
