"""Worktree merge and push operations.

Handles merging worktree commits into main and pushing to origin.
Separated from ``worktree.py`` (which handles creation, sync, and startup
reconciliation) to keep each module focused on a single concern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pynchy.host.git_ops.repo import RepoContext
from pynchy.host.git_ops.utils import detect_main_branch, git_env_with_token, push_local_commits, run_git
from pynchy.host.git_ops.worktree import _safe_rebase
from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.types import WorkspaceProfile


def merge_worktree(group_folder: str, repo_ctx: RepoContext) -> bool:
    """Rebase worktree commits onto main, then fast-forward merge.

    Uses rebase-then-merge so worktree commits land on main even when main
    has advanced from another worktree's merge (where plain --ff-only would fail).

    The rebase runs from within the worktree directory because git won't check
    out a branch that's already checked out in another worktree.

    Args:
        group_folder: Group folder name (e.g. "code-improver")
        repo_ctx: Resolved repo context

    Returns:
        True if merge succeeded or nothing to merge, False on conflict
    """
    branch_name = f"worktree/{group_folder}"
    worktree_path = repo_ctx.worktrees_dir / group_folder
    main_branch = detect_main_branch(cwd=repo_ctx.root)

    # Check if worktree branch has commits ahead of HEAD
    count = run_git("rev-list", f"HEAD..{branch_name}", "--count", cwd=repo_ctx.root)
    if count.returncode != 0:
        logger.warning(
            "Failed to check worktree commits",
            group=group_folder,
            error=count.stderr.strip(),
        )
        return False

    try:
        ahead = int(count.stdout.strip())
    except (ValueError, TypeError):
        logger.warning(
            "Failed to parse worktree commit count",
            group=group_folder,
            stdout=count.stdout.strip(),
        )
        return False

    if ahead == 0:
        logger.debug("Nothing to merge from worktree", group=group_folder)
        return True

    # Rebase from within the worktree so the branch is already checked out
    # (git refuses to check out a branch used by another worktree)
    if not _safe_rebase(main_branch, cwd=worktree_path):
        logger.warning("Worktree rebase failed", group=group_folder)
        return False

    # Now ff-only merge is guaranteed to succeed
    merge = run_git("merge", "--ff-only", branch_name, cwd=repo_ctx.root)
    if merge.returncode != 0:
        logger.warning(
            "Worktree merge failed after rebase",
            group=group_folder,
            error=merge.stderr.strip(),
        )
        return False

    logger.info(
        "Worktree commits merged",
        group=group_folder,
        commits=ahead,
    )
    return True


def merge_and_push_worktree(group_folder: str, repo_ctx: RepoContext) -> None:
    """Merge worktree commits into main and push to origin.

    Combines merge_worktree() + push_local_commits() into a single call.
    Designed to run in a thread via asyncio.to_thread().
    """
    if merge_worktree(group_folder, repo_ctx):
        env = git_env_with_token(repo_ctx.slug)
        push_local_commits(cwd=repo_ctx.root, env=env)


async def merge_worktree_with_policy(group_folder: str) -> None:
    """Await a policy-aware worktree merge.

    Resolves the group's git_policy, then runs the appropriate workflow:
    - merge-to-main: merge into main and push
    - pull-request: push branch to origin and open/update a PR

    Blocks until the merge completes.  For fire-and-forget semantics,
    use background_merge_worktree() instead.
    """
    import asyncio

    from pynchy.host.git_ops.repo import resolve_repo_for_group
    from pynchy.host.git_ops.sync import (
        GIT_POLICY_PR,
        host_create_pr_from_worktree,
        resolve_git_policy,
    )

    repo_ctx = resolve_repo_for_group(group_folder)
    if repo_ctx is None:
        return

    policy = resolve_git_policy(group_folder)

    if policy == GIT_POLICY_PR:
        await asyncio.to_thread(host_create_pr_from_worktree, group_folder, repo_ctx)
    else:
        await asyncio.to_thread(merge_and_push_worktree, group_folder, repo_ctx)


def background_merge_worktree(group: WorkspaceProfile) -> None:
    """Fire-and-forget worktree merge for groups with repo access.

    Thin wrapper around merge_worktree_with_policy() that runs the merge
    in a background task.  This is the preferred entry point for
    post-session merges where the caller doesn't need to wait.
    """
    from pynchy.utils import create_background_task

    create_background_task(
        merge_worktree_with_policy(group.folder),
        name=f"worktree-merge-{group.folder}",
    )
