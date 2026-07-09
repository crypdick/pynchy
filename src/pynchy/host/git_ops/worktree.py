"""Git worktree creation, sync, and startup reconciliation.

Non-admin groups with repo_access get their own git worktree instead of
mounting the shared project root. Worktrees share the git object store
(near-zero disk overhead) but have fully independent working trees and indexes.

Design: existing worktrees use best-effort sync (fetch + merge), never
``git reset --hard``. A service restart kills all running containers, so
agents may leave uncommitted work in their worktree. We preserve that state
and notify the agent via system notices so it can resume gracefully.

Merge and push operations live in ``_worktree_merge.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pynchy.host.git_ops.repo import RepoContext, repo_container_path
from pynchy.host.git_ops.utils import (
    count_commits,
    detect_main_branch,
    git_env_with_token,
    run_git,
)
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

    When absent: creates from origin/{main}. Raises WorktreeError on failure.

    For existing worktrees: best-effort pull (fetch + merge). Uncommitted changes
    are preserved and reported via notices so the agent can resume gracefully
    after a service restart.

    Args:
        group_folder: Group folder name (e.g. "code-improver")
        repo_ctx: Resolved repo context (root path, worktrees dir)

    Returns:
        WorktreeResult with path and any system notices for the agent

    Raises:
        WorktreeError: If creating a worktree fails
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
    env = git_env_with_token(repo_ctx.slug, group_folder=group_folder)

    # Check for uncommitted changes
    status = run_git("status", "--porcelain", cwd=worktree_path)
    if status.returncode == 0 and status.stdout.strip():
        notices.append(
            "Your worktree has uncommitted changes from a previous run. "
            f"Review with `git status` and `git diff` in {repo_container_path(repo_ctx.slug)} — "
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
                    f"Run `git log --oneline` in {repo_container_path(repo_ctx.slug)} "
                    "to see what changed."
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
    """Create a worktree from origin/{main}. Raises WorktreeError on failure."""
    env = git_env_with_token(repo_ctx.slug, group_folder=group_folder)
    # Fetch is required for initial creation
    fetch = run_git("fetch", "origin", cwd=repo_ctx.root, env=env)
    if fetch.returncode != 0:
        raise WorktreeError(f"git fetch failed: {fetch.stderr.strip()}")

    repo_ctx.worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Clean up stale worktree entries and branches
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
    on PATH when the configured venv isn't available (e.g. inside containers).
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
            check=False,
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
    """Move worktrees into the unified data/worktrees/ structure.

    Source: ~/.config/pynchy/worktrees/<folder>/
    Destination: data/worktrees/<owner>/<repo>/<folder>/

    Attempts `git worktree move` first; falls back to deleting the source entry
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
            continue  # destination already exists

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
            # Move failed (e.g. git lacks `worktree move`) — remove and let reconcile recreate
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
    from pynchy.host.git_ops.repo import (
        check_token_expiry,
        ensure_repo_cloned,
        get_repo_context,
        get_repo_token,
    )

    repo_groups = repo_groups or {}

    # Source base path worktrees are relocated from
    s = get_settings()
    old_base = s.home_dir / ".config" / "pynchy" / "worktrees"

    for slug, folders in repo_groups.items():
        repo_ctx = _startup_repo_context(s, slug, get_repo_context)
        if repo_ctx is None:
            continue
        _warn_if_repo_token_missing(s, slug, check_token_expiry, get_repo_token)
        if not _prepare_repo_for_startup(
            repo_ctx,
            slug,
            ensure_repo_cloned,
            old_base,
            s.project_root,
        ):
            continue
        _ensure_startup_worktrees(slug, folders, repo_ctx)
        _rebase_diverged_worktrees(repo_ctx)


def _startup_repo_context(
    _settings,
    slug: str,
    get_repo_context: Callable[[str], RepoContext | None],
) -> RepoContext | None:
    repo_ctx = get_repo_context(slug)
    if repo_ctx is None:
        logger.warning("Slug not configured in [repos], skipping", slug=slug)
    return repo_ctx


def _warn_if_repo_token_missing(
    settings,
    slug: str,
    check_token_expiry,
    get_repo_token,
) -> None:
    repo_cfg = settings.repos.overrides.get(slug)
    if repo_cfg and repo_cfg.token:
        check_token_expiry(slug, repo_cfg.token.get_secret_value())
        return
    if get_repo_token(slug):
        return
    logger.warning(
        "No git token for repo — private repos will fail to clone",
        slug=slug,
    )


def _prepare_repo_for_startup(
    repo_ctx: RepoContext,
    slug: str,
    ensure_repo_cloned,
    old_base: Path,
    project_root: Path,
) -> bool:
    if not ensure_repo_cloned(repo_ctx):
        logger.warning("Repo not available, skipping worktree reconciliation", slug=slug)
        return False

    run_git("worktree", "prune", cwd=repo_ctx.root)
    install_pre_commit_hooks(repo_ctx.root)
    if repo_ctx.root.resolve() == project_root.resolve():
        _migrate_old_worktrees(repo_ctx, old_base)
    return True


def _ensure_startup_worktrees(slug: str, folders: list[str], repo_ctx: RepoContext) -> None:
    for folder in folders:
        try:
            ensure_worktree(folder, repo_ctx)
        except WorktreeError:
            logger.warning("Failed to create worktree at startup", group=folder, slug=slug)


def _rebase_diverged_worktrees(repo_ctx: RepoContext) -> None:
    if not repo_ctx.worktrees_dir.exists():
        return

    main_branch = detect_main_branch(cwd=repo_ctx.root)
    for entry in sorted(repo_ctx.worktrees_dir.iterdir()):
        if not entry.is_dir():
            continue
        _rebase_diverged_worktree(repo_ctx, entry, main_branch)


def _rebase_diverged_worktree(
    repo_ctx: RepoContext,
    entry: Path,
    main_branch: str,
) -> None:
    group_folder = entry.name
    branch_name = f"worktree/{group_folder}"

    if not _branch_exists(repo_ctx.root, branch_name):
        logger.debug("Worktree branch missing, skipping", group=group_folder)
        return

    divergence = _worktree_divergence(repo_ctx.root, main_branch, branch_name)
    if divergence is None:
        logger.warning("Failed to check worktree divergence", group=group_folder)
        return

    ahead_count, behind_count = divergence
    if ahead_count == 0 or behind_count == 0:
        return

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


def _branch_exists(repo_root: Path, branch_name: str) -> bool:
    branch_check = run_git("rev-parse", "--verify", branch_name, cwd=repo_root)
    return branch_check.returncode == 0


def _worktree_divergence(
    repo_root: Path,
    main_branch: str,
    branch_name: str,
) -> tuple[int, int] | None:
    ahead_count = count_commits(f"{main_branch}..{branch_name}", cwd=repo_root)
    behind_count = count_commits(f"{branch_name}..{main_branch}", cwd=repo_root)
    if ahead_count is None or behind_count is None:
        return None
    return ahead_count, behind_count
