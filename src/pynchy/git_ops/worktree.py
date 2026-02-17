"""Git worktree management for container isolation.

Non-god groups with project_access get their own git worktree instead of
mounting the shared project root. Worktrees share the git object store
(near-zero disk overhead) but have fully independent working trees and indexes.

Design: existing worktrees use best-effort sync (fetch + merge), never
``git reset --hard``. A service restart kills all running containers, so
agents may leave uncommitted work in their worktree. We preserve that state
and notify the agent via system notices so it can resume gracefully.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from pynchy.config import get_settings
from pynchy.git_ops.utils import detect_main_branch, push_local_commits, run_git
from pynchy.logger import logger


class WorktreeError(Exception):
    """Failed to create or sync a git worktree."""


def _safe_rebase(target_branch: str, *, cwd: Path) -> bool:
    """Rebase onto target_branch, aborting on conflict.

    Returns True if rebase succeeded, False if it conflicted (and was aborted).
    """
    rebase = run_git("rebase", target_branch, cwd=cwd)
    if rebase.returncode != 0:
        run_git("rebase", "--abort", cwd=cwd)
        return False
    return True


@dataclass
class WorktreeResult:
    """Result of ensure_worktree — path plus any notices for the agent."""

    path: Path
    notices: list[str] = field(default_factory=list)


def ensure_worktree(group_folder: str) -> WorktreeResult:
    """Ensure a git worktree exists for the given group.

    For new worktrees: creates from origin/{main}. Raises WorktreeError on failure.

    For existing worktrees: best-effort pull (fetch + merge). Uncommitted changes
    are preserved and reported via notices so the agent can resume gracefully
    after a service restart.

    Args:
        group_folder: Group folder name (e.g. "code-improver")

    Returns:
        WorktreeResult with path and any system notices for the agent

    Raises:
        WorktreeError: If creating a new worktree fails
    """
    worktree_path = get_settings().worktrees_dir / group_folder
    # Use worktree/ prefix to avoid ref conflicts (e.g. "main/workspace" would
    # conflict with the "main" branch since git refs are path-based).
    branch_name = f"worktree/{group_folder}"
    main_branch = detect_main_branch()

    if worktree_path.exists():
        # Health check: verify the worktree is a functional git repo.
        # A stale .git reference (e.g. from a group rename) makes the
        # directory look like a worktree but git commands silently fail.
        health = run_git("rev-parse", "--git-dir", cwd=worktree_path)
        if health.returncode != 0:
            logger.warning(
                "Broken worktree detected, recreating",
                group=group_folder,
                error=health.stderr.strip(),
            )
            shutil.rmtree(worktree_path)
            # Fall through to create path below
        else:
            return _sync_existing_worktree(worktree_path, group_folder, main_branch)

    return _create_new_worktree(worktree_path, group_folder, branch_name, main_branch)


def _sync_existing_worktree(
    worktree_path: Path, group_folder: str, main_branch: str
) -> WorktreeResult:
    """Sync an existing worktree — best-effort pull, preserve local state."""
    notices: list[str] = []

    # Check for uncommitted changes
    status = run_git("status", "--porcelain", cwd=worktree_path)
    if status.returncode == 0 and status.stdout.strip():
        notices.append(
            "Your worktree has uncommitted changes from a previous run. "
            "Review with `git status` and `git diff` in /workspace/project — "
            "commit or discard them before starting new work."
        )
        logger.info("Worktree has uncommitted changes", group=group_folder)

    # Best-effort fetch + merge
    fetch = run_git("fetch", "origin")
    if fetch.returncode != 0:
        notices.append(
            f"Failed to pull latest changes: git fetch failed ({fetch.stderr.strip()}). "
            "Proceeding with existing worktree state."
        )
        logger.warning("Worktree fetch failed", group=group_folder, error=fetch.stderr.strip())
    else:
        head_before = run_git("rev-parse", "HEAD", cwd=worktree_path).stdout.strip()
        merge = run_git("merge", "--no-edit", f"origin/{main_branch}", cwd=worktree_path)
        if merge.returncode != 0:
            notices.append(
                f"Failed to pull latest changes: merge of origin/{main_branch} failed "
                f"({merge.stderr.strip()}). Proceeding with existing worktree state."
            )
            logger.warning("Worktree merge failed", group=group_folder, error=merge.stderr.strip())
        else:
            head_after = run_git("rev-parse", "HEAD", cwd=worktree_path).stdout.strip()
            if head_before != head_after:
                notices.append(
                    f"Auto-pulled remote changes from origin/{main_branch} into your worktree. "
                    "Run `git log --oneline` in /workspace/project to see what changed."
                )
            logger.info("Worktree synced", group=group_folder, path=str(worktree_path))

    return WorktreeResult(path=worktree_path, notices=notices)


def _create_new_worktree(
    worktree_path: Path, group_folder: str, branch_name: str, main_branch: str
) -> WorktreeResult:
    """Create a new worktree from origin/{main}. Raises WorktreeError on failure."""
    # Fetch is required for initial creation
    fetch = run_git("fetch", "origin")
    if fetch.returncode != 0:
        raise WorktreeError(f"git fetch failed: {fetch.stderr.strip()}")

    get_settings().worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Clean up stale worktree entries and branches from previous runs
    run_git("worktree", "prune")
    run_git("branch", "-D", branch_name)

    add = run_git(
        "worktree",
        "add",
        "-b",
        branch_name,
        str(worktree_path),
        f"origin/{main_branch}",
    )
    if add.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {add.stderr.strip()}")

    logger.info(
        "Worktree created",
        group=group_folder,
        branch=branch_name,
        path=str(worktree_path),
    )
    return WorktreeResult(path=worktree_path)


def reconcile_worktrees_at_startup(
    project_access_folders: list[str] | None = None,
) -> None:
    """Ensure worktrees exist for all project_access groups, then rebase diverged branches.

    Called at startup before any containers launch. Creates missing worktrees
    so the git sync loop can notify all groups from boot, and rebases diverged
    branches for clean ff-merges after the next container run.
    """
    # Clean git's internal stale entries (worktree dirs that no longer exist)
    run_git("worktree", "prune")

    # Create missing worktrees for known project_access groups.
    # ensure_worktree's health check handles broken worktrees automatically.
    for folder in project_access_folders or []:
        try:
            ensure_worktree(folder)
        except WorktreeError:
            logger.warning("Failed to create worktree at startup", group=folder)

    if not get_settings().worktrees_dir.exists():
        return

    main_branch = detect_main_branch()

    for entry in sorted(get_settings().worktrees_dir.iterdir()):
        if not entry.is_dir():
            continue

        group_folder = entry.name
        branch_name = f"worktree/{group_folder}"

        # Check if branch exists
        branch_check = run_git("rev-parse", "--verify", branch_name)
        if branch_check.returncode != 0:
            logger.debug("Worktree branch missing, skipping", group=group_folder)
            continue

        # Check divergence: commits ahead and behind main
        ahead = run_git("rev-list", f"{main_branch}..{branch_name}", "--count")
        behind = run_git("rev-list", f"{branch_name}..{main_branch}", "--count")

        if ahead.returncode != 0 or behind.returncode != 0:
            logger.warning("Failed to check worktree divergence", group=group_folder)
            continue

        try:
            ahead_count = int(ahead.stdout.strip())
            behind_count = int(behind.stdout.strip())
        except (ValueError, TypeError):
            logger.warning("Failed to parse worktree divergence count", group=group_folder)
            continue

        if ahead_count == 0 or behind_count == 0:
            # Not diverged — either up to date or simply ahead (will ff-merge fine)
            continue

        logger.info(
            "Worktree diverged from main, rebasing",
            group=group_folder,
            ahead=ahead_count,
            behind=behind_count,
        )

        # Rebase from within the worktree (git won't check out a branch
        # that's already checked out in another worktree)
        if _safe_rebase(main_branch, cwd=entry):
            logger.info("Worktree rebased onto main at startup", group=group_folder)
        else:
            logger.warning(
                "Startup worktree rebase failed (needs manual resolution)",
                group=group_folder,
            )


def merge_worktree(group_folder: str) -> bool:
    """Rebase worktree commits onto main, then fast-forward merge.

    Uses rebase-then-merge so worktree commits land on main even when main
    has advanced from another worktree's merge (where plain --ff-only would fail).

    The rebase runs from within the worktree directory because git won't check
    out a branch that's already checked out in another worktree.

    Args:
        group_folder: Group folder name (e.g. "code-improver")

    Returns:
        True if merge succeeded or nothing to merge, False on conflict
    """
    branch_name = f"worktree/{group_folder}"
    worktree_path = get_settings().worktrees_dir / group_folder
    main_branch = detect_main_branch()

    # Check if worktree branch has commits ahead of HEAD
    count = run_git("rev-list", f"HEAD..{branch_name}", "--count")
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
    merge = run_git("merge", "--ff-only", branch_name)
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


def merge_and_push_worktree(group_folder: str) -> None:
    """Merge worktree commits into main and push to origin.

    Combines merge_worktree() + push_local_commits() into a single call.
    Designed to run in a thread via asyncio.to_thread().
    """
    if merge_worktree(group_folder):
        push_local_commits()


def background_merge_worktree(group: object) -> None:
    """Fire-and-forget worktree merge for groups with project access.

    Checks has_project_access, then runs merge_and_push_worktree in a
    background thread. This is the single code path for all post-session
    worktree merges (message handler, session handler, IPC, scheduler).

    Args:
        group: A RegisteredGroup (or any object with a .folder attribute).
    """
    import asyncio

    from pynchy.utils import create_background_task
    from pynchy.workspace_config import has_project_access

    if not has_project_access(group):  # type: ignore[arg-type]
        return

    folder: str = group.folder  # type: ignore[union-attr]
    create_background_task(
        asyncio.to_thread(merge_and_push_worktree, folder),
        name=f"worktree-merge-{folder}",
    )
