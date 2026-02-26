"""Git worktree management for container isolation.

Non-admin groups with repo_access get their own git worktree instead of
mounting the shared project root. Worktrees share the git object store
(near-zero disk overhead) but have fully independent working trees and indexes.

Design: existing worktrees use best-effort sync (fetch + merge), never
``git reset --hard``. A service restart kills all running containers, so
agents may leave uncommitted work in their worktree. We preserve that state
and notify the agent via system notices so it can resume gracefully.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pynchy.git_ops.repo import RepoContext
from pynchy.git_ops.utils import detect_main_branch, git_env_with_token, push_local_commits, run_git
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


def ensure_worktree(group_folder: str, repo_ctx: RepoContext) -> WorktreeResult:
    """Ensure a git worktree exists for the given group.

    For new worktrees: creates from origin/{main}. Raises WorktreeError on failure.

    For existing worktrees: best-effort pull (fetch + merge). Uncommitted changes
    are preserved and reported via notices so the agent can resume gracefully
    after a service restart.

    Args:
        group_folder: Group folder name (e.g. "code-improver")
        repo_ctx: Resolved repo context (root path, worktrees dir)

    Returns:
        WorktreeResult with path and any system notices for the agent

    Raises:
        WorktreeError: If creating a new worktree fails
    """
    worktree_path = repo_ctx.worktrees_dir / group_folder
    # Use worktree/ prefix to avoid ref conflicts (e.g. "main/workspace" would
    # conflict with the "main" branch since git refs are path-based).
    branch_name = f"worktree/{group_folder}"
    main_branch = detect_main_branch(cwd=repo_ctx.root)

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
            return _sync_existing_worktree(worktree_path, group_folder, main_branch, repo_ctx)

    return _create_new_worktree(worktree_path, group_folder, branch_name, main_branch, repo_ctx)


def _sync_existing_worktree(
    worktree_path: Path, group_folder: str, main_branch: str, repo_ctx: RepoContext
) -> WorktreeResult:
    """Sync an existing worktree — best-effort pull, preserve local state."""
    notices: list[str] = []
    env = git_env_with_token(repo_ctx.slug)

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
    fetch = run_git("fetch", "origin", cwd=repo_ctx.root, env=env)
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
    worktree_path: Path,
    group_folder: str,
    branch_name: str,
    main_branch: str,
    repo_ctx: RepoContext,
) -> WorktreeResult:
    """Create a new worktree from origin/{main}. Raises WorktreeError on failure."""
    env = git_env_with_token(repo_ctx.slug)
    # Fetch is required for initial creation
    fetch = run_git("fetch", "origin", cwd=repo_ctx.root, env=env)
    if fetch.returncode != 0:
        raise WorktreeError(f"git fetch failed: {fetch.stderr.strip()}")

    repo_ctx.worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Clean up stale worktree entries and branches from previous runs
    run_git("worktree", "prune", cwd=repo_ctx.root)
    run_git("branch", "-D", branch_name, cwd=repo_ctx.root)

    add = run_git(
        "worktree",
        "add",
        "-b",
        branch_name,
        str(worktree_path),
        f"origin/{main_branch}",
        cwd=repo_ctx.root,
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


def install_pre_commit_hooks(repo_root: Path) -> None:
    """Ensure pre-commit hooks are installed in the repo's .git/hooks/.

    Git worktrees share hooks from the main repo, so installing once covers
    all agent workspaces. The generated hook script falls back to ``pre-commit``
    on PATH when the original venv isn't available (e.g. inside containers).
    """
    config = repo_root / ".pre-commit-config.yaml"
    if not config.exists():
        return

    try:
        result = subprocess.run(
            ["uv", "run", "pre-commit", "install"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("Pre-commit hooks installed", repo=str(repo_root))
        else:
            logger.warning(
                "pre-commit install failed (workspace unaffected)",
                repo=str(repo_root),
                stderr=result.stderr.strip(),
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "pre-commit install error (workspace unaffected)",
            repo=str(repo_root),
            err=str(exc),
        )


def _migrate_old_worktrees(repo_ctx: RepoContext, old_base: Path) -> None:
    """Migrate existing worktrees from old location to new unified structure.

    Old path: ~/.config/pynchy/worktrees/<folder>/
    New path: data/worktrees/<owner>/<repo>/<folder>/

    Attempts `git worktree move` first; falls back to deleting the old entry
    so reconcile_worktrees_at_startup can recreate it from the branch.
    """
    if not old_base.exists():
        return

    for entry in sorted(old_base.iterdir()):
        if not entry.is_dir():
            continue
        # Confirm it's actually a git worktree (has .git file)
        if not (entry / ".git").exists():
            continue

        new_path = repo_ctx.worktrees_dir / entry.name
        if new_path.exists():
            continue  # already migrated

        new_path.parent.mkdir(parents=True, exist_ok=True)
        move = run_git("worktree", "move", str(entry), str(new_path), cwd=repo_ctx.root)
        if move.returncode == 0:
            logger.info(
                "Migrated worktree to new location",
                group=entry.name,
                old=str(entry),
                new=str(new_path),
            )
        else:
            # Move failed (e.g. git version too old) — remove and let reconcile recreate
            logger.warning(
                "Worktree move failed, removing for recreation",
                group=entry.name,
                error=move.stderr.strip(),
            )
            remove = run_git("worktree", "remove", "--force", str(entry), cwd=repo_ctx.root)
            if remove.returncode != 0:
                logger.warning(
                    "git worktree remove failed, cleaning up manually",
                    group=entry.name,
                )
                shutil.rmtree(entry, ignore_errors=True)


def reconcile_worktrees_at_startup(
    repo_groups: dict[str, list[str]] | None = None,
) -> None:
    """Ensure worktrees exist for all repo_access groups, then rebase diverged branches.

    Called at startup before any containers launch. Creates missing worktrees
    so the git sync loop can notify all groups from boot, and rebases diverged
    branches for clean ff-merges after the next container run.

    Args:
        repo_groups: Dict mapping slug → list of group folder names.
    """
    from pynchy.config import get_settings
    from pynchy.git_ops.repo import check_token_expiry, ensure_repo_cloned, get_repo_context, get_repo_token

    repo_groups = repo_groups or {}

    # Old worktrees base (pre-migration)
    s = get_settings()
    old_base = s.home_dir / ".config" / "pynchy" / "worktrees"

    for slug, folders in repo_groups.items():
        repo_ctx = get_repo_context(slug)
        if repo_ctx is None:
            logger.warning("Slug not configured in [repos], skipping", slug=slug)
            continue

        # Validate token availability and expiry
        repo_cfg = s.repos.get(slug)
        if repo_cfg and repo_cfg.token:
            check_token_expiry(slug, repo_cfg.token.get_secret_value())
        elif not get_repo_token(slug):
            logger.warning(
                "No git token for repo — private repos will fail to clone",
                slug=slug,
            )

        # Clone auto-managed repos if they don't exist yet
        if not ensure_repo_cloned(repo_ctx):
            logger.warning("Repo not available, skipping worktree reconciliation", slug=slug)
            continue

        # Clean git's internal stale entries
        run_git("worktree", "prune", cwd=repo_ctx.root)

        # Install pre-commit hooks for this repo
        install_pre_commit_hooks(repo_ctx.root)

        # Migrate pynchy's own worktrees from the old ~/.config/pynchy/worktrees/ path
        if repo_ctx.root.resolve() == s.project_root.resolve():
            _migrate_old_worktrees(repo_ctx, old_base)

        # Create missing worktrees for known repo_access groups.
        for folder in folders:
            try:
                ensure_worktree(folder, repo_ctx)
            except WorktreeError:
                logger.warning("Failed to create worktree at startup", group=folder, slug=slug)

        if not repo_ctx.worktrees_dir.exists():
            continue

        main_branch = detect_main_branch(cwd=repo_ctx.root)

        for entry in sorted(repo_ctx.worktrees_dir.iterdir()):
            if not entry.is_dir():
                continue

            group_folder = entry.name
            branch_name = f"worktree/{group_folder}"

            # Check if branch exists
            branch_check = run_git("rev-parse", "--verify", branch_name, cwd=repo_ctx.root)
            if branch_check.returncode != 0:
                logger.debug("Worktree branch missing, skipping", group=group_folder)
                continue

            # Check divergence: commits ahead and behind main
            ahead = run_git(
                "rev-list", f"{main_branch}..{branch_name}", "--count", cwd=repo_ctx.root
            )
            behind = run_git(
                "rev-list", f"{branch_name}..{main_branch}", "--count", cwd=repo_ctx.root
            )

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

            if _safe_rebase(main_branch, cwd=entry):
                logger.info("Worktree rebased onto main at startup", group=group_folder)
            else:
                logger.warning(
                    "Startup worktree rebase failed (needs manual resolution)",
                    group=group_folder,
                )


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

    from pynchy.git_ops.repo import resolve_repo_for_group
    from pynchy.git_ops.sync import (
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


def background_merge_worktree(group: object) -> None:
    """Fire-and-forget worktree merge for groups with repo access.

    Thin wrapper around merge_worktree_with_policy() that runs the merge
    in a background task.  This is the preferred entry point for
    post-session merges where the caller doesn't need to wait.

    Args:
        group: A WorkspaceProfile (or any object with a .folder attribute).
    """
    from pynchy.utils import create_background_task

    folder: str = group.folder  # type: ignore[union-attr]
    create_background_task(
        merge_worktree_with_policy(folder),
        name=f"worktree-merge-{folder}",
    )
