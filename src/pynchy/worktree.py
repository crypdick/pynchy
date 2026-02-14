"""Git worktree management for container isolation.

Non-main groups with project_access get their own git worktree instead of
mounting the shared project root. Worktrees share the git object store
(near-zero disk overhead) but have fully independent working trees and indexes.

Design: existing worktrees use best-effort sync (fetch + merge), never
``git reset --hard``. A service restart kills all running containers, so
agents may leave uncommitted work in their worktree. We preserve that state
and notify the agent via system notices so it can resume gracefully.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pynchy.config import PROJECT_ROOT, WORKTREES_DIR
from pynchy.logger import logger

_SUBPROCESS_TIMEOUT = 30


class WorktreeError(Exception):
    """Failed to create or sync a git worktree."""


@dataclass
class WorktreeResult:
    """Result of ensure_worktree — path plus any notices for the agent."""

    path: Path
    notices: list[str] = field(default_factory=list)


def _run_git(
    *args: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with standard timeout and error capture."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )


def _detect_main_branch() -> str:
    """Detect the main branch name via origin/HEAD, fallback to 'main'."""
    result = _run_git("symbolic-ref", "refs/remotes/origin/HEAD")
    if result.returncode == 0:
        # Output like "refs/remotes/origin/main"
        ref = result.stdout.strip()
        return ref.split("/")[-1]
    return "main"


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
    worktree_path = WORKTREES_DIR / group_folder
    # Use worktree/ prefix to avoid ref conflicts (e.g. "main/workspace" would
    # conflict with the "main" branch since git refs are path-based).
    branch_name = f"worktree/{group_folder}"
    main_branch = _detect_main_branch()

    if worktree_path.exists():
        return _sync_existing_worktree(worktree_path, group_folder, main_branch)

    return _create_new_worktree(worktree_path, group_folder, branch_name, main_branch)


def _sync_existing_worktree(
    worktree_path: Path, group_folder: str, main_branch: str
) -> WorktreeResult:
    """Sync an existing worktree — best-effort pull, preserve local state."""
    notices: list[str] = []

    # Check for uncommitted changes
    status = _run_git("status", "--porcelain", cwd=worktree_path)
    if status.returncode == 0 and status.stdout.strip():
        notices.append(
            "Your worktree has uncommitted changes from a previous run. "
            "Review with `git status` and `git diff` in /workspace/project — "
            "commit or discard them before starting new work."
        )
        logger.info("Worktree has uncommitted changes", group=group_folder)

    # Best-effort fetch + merge
    fetch = _run_git("fetch", "origin")
    if fetch.returncode != 0:
        notices.append(
            f"Failed to pull latest changes: git fetch failed ({fetch.stderr.strip()}). "
            "Proceeding with existing worktree state."
        )
        logger.warning("Worktree fetch failed", group=group_folder, error=fetch.stderr.strip())
    else:
        head_before = _run_git("rev-parse", "HEAD", cwd=worktree_path).stdout.strip()
        merge = _run_git("merge", "--no-edit", f"origin/{main_branch}", cwd=worktree_path)
        if merge.returncode != 0:
            notices.append(
                f"Failed to pull latest changes: merge of origin/{main_branch} failed "
                f"({merge.stderr.strip()}). Proceeding with existing worktree state."
            )
            logger.warning("Worktree merge failed", group=group_folder, error=merge.stderr.strip())
        else:
            head_after = _run_git("rev-parse", "HEAD", cwd=worktree_path).stdout.strip()
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
    fetch = _run_git("fetch", "origin")
    if fetch.returncode != 0:
        raise WorktreeError(f"git fetch failed: {fetch.stderr.strip()}")

    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)

    # Clean up stale worktree entries and branches from previous runs
    _run_git("worktree", "prune")
    _run_git("branch", "-D", branch_name)

    add = _run_git(
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


def cleanup_stale_worktrees() -> None:
    """Prune stale git worktree entries and rebase diverged branches onto main.

    Called at startup before any containers launch. Ensures worktrees are
    ready for clean ff-merges after the next container run.
    """
    # Clean git's internal stale entries (worktree dirs that no longer exist)
    _run_git("worktree", "prune")

    if not WORKTREES_DIR.exists():
        return

    main_branch = _detect_main_branch()

    for entry in sorted(WORKTREES_DIR.iterdir()):
        if not entry.is_dir():
            continue

        group_folder = entry.name
        branch_name = f"worktree/{group_folder}"

        # Check if branch exists
        branch_check = _run_git("rev-parse", "--verify", branch_name)
        if branch_check.returncode != 0:
            logger.debug("Worktree branch missing, skipping", group=group_folder)
            continue

        # Check divergence: commits ahead and behind main
        ahead = _run_git("rev-list", f"{main_branch}..{branch_name}", "--count")
        behind = _run_git("rev-list", f"{branch_name}..{main_branch}", "--count")

        if ahead.returncode != 0 or behind.returncode != 0:
            logger.warning("Failed to check worktree divergence", group=group_folder)
            continue

        ahead_count = int(ahead.stdout.strip())
        behind_count = int(behind.stdout.strip())

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
        rebase = _run_git("rebase", main_branch, cwd=entry)
        if rebase.returncode != 0:
            _run_git("rebase", "--abort", cwd=entry)
            logger.warning(
                "Startup worktree rebase failed (needs manual resolution)",
                group=group_folder,
                error=rebase.stderr.strip(),
            )
        else:
            logger.info("Worktree rebased onto main at startup", group=group_folder)


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
    worktree_path = WORKTREES_DIR / group_folder
    main_branch = _detect_main_branch()

    # Check if worktree branch has commits ahead of HEAD
    count = _run_git("rev-list", f"HEAD..{branch_name}", "--count")
    if count.returncode != 0:
        logger.warning(
            "Failed to check worktree commits",
            group=group_folder,
            error=count.stderr.strip(),
        )
        return False

    ahead = int(count.stdout.strip())
    if ahead == 0:
        logger.debug("Nothing to merge from worktree", group=group_folder)
        return True

    # Rebase from within the worktree so the branch is already checked out
    # (git refuses to check out a branch used by another worktree)
    rebase = _run_git("rebase", main_branch, cwd=worktree_path)
    if rebase.returncode != 0:
        _run_git("rebase", "--abort", cwd=worktree_path)
        logger.warning(
            "Worktree rebase failed",
            group=group_folder,
            error=rebase.stderr.strip(),
        )
        return False

    # Now ff-only merge is guaranteed to succeed
    merge = _run_git("merge", "--ff-only", branch_name)
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
