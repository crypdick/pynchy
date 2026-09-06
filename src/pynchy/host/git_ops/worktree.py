"""Git worktree creation, sync, and startup reconciliation.

Non-admin groups with repo_access get their own git worktree instead of
mounting the shared project root. Worktrees share the git object store
(near-zero disk overhead) but have fully independent working trees and indexes.

Design: existing worktrees use best-effort sync (fetch + merge), never
``git reset --hard``. A service restart kills all running containers, so
agents may leave uncommitted work in their worktree. We preserve that state
and notify the agent via system notices so it can resume gracefully.

Agents publish committed changes explicitly through ``sync_worktree_to_main``.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - worktree helper uses fixed no-shell hook-runner argv.
from collections.abc import (
    Callable,
    Sequence,
)
from dataclasses import dataclass
from pathlib import Path  # beartype resolves worktree signatures at runtime.

import pynchy.host.git_ops.repo as repo_manager
from pynchy.host.git_ops import _routed_host_worktree
from pynchy.host.git_ops._worktree_models import (
    RoutedHostWorktreeResult,
    WorktreeError,
    WorktreeResult,
)
from pynchy.host.git_ops.repo import RepoContext, repo_container_path
from pynchy.host.git_ops.utils import (
    count_commits,
    detect_main_branch,
    git_env_with_token,
    git_env_without_credentials,
    run_git,
)
from pynchy.host.git_ops.worktree_venv import mark_worktree_used
from pynchy.logger import logger

_GIT_FETCH_FAILED = "git fetch failed: {stderr}"
_GIT_WORKTREE_ADD_FAILED = "git worktree add failed: {stderr}"


@dataclass(frozen=True)
class WorktreeStartupRuntime:
    """Resolved host paths and configured repository tokens for startup."""

    home_dir: Path
    project_root: Path
    configured_tokens: dict[str, str | None]


_runtime: WorktreeStartupRuntime = WorktreeStartupRuntime(Path.home(), Path.cwd(), {})


def configure_worktree_startup_runtime(runtime: WorktreeStartupRuntime) -> None:
    """Set worktree startup configuration from the composition root."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _safe_rebase(target_branch: str, *, cwd: Path) -> bool:
    """Rebase onto target_branch, aborting on conflict.

    Returns True if rebase succeeded, False if it conflicted (and was aborted).
    """
    git_dir = run_git("rev-parse", "--absolute-git-dir", cwd=cwd)
    if git_dir.returncode != 0 or not git_dir.stdout.strip():
        return False
    if any(
        (Path(git_dir.stdout.strip()) / marker).exists()
        for marker in ("rebase-apply", "rebase-merge")
    ):
        logger.info("Worktree rebase already in progress; preserving agent state", path=str(cwd))
        return False
    rebase = run_git("rebase", target_branch, cwd=cwd)
    if rebase.returncode != 0:
        run_git("rebase", "--abort", cwd=cwd)
        return False
    return True


def ensure_worktree(
    group_folder: str, repo_ctx: RepoContext, *, mark_used: bool = True
) -> WorktreeResult:
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
    if worktree_path.is_symlink():
        raise WorktreeError(
            "Worktree path is a symbolic link; leave it untouched and recover it manually."
        )
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
            raise WorktreeError(
                "Worktree Git metadata is invalid; leave its files untouched "
                "and recover it manually."
            )
        result = _sync_existing_worktree(worktree_path, group_folder, main_branch, repo_ctx)
        if mark_used:
            mark_worktree_used(result.path)
        return result

    result = _create_new_worktree(worktree_path, group_folder, branch_name, main_branch, repo_ctx)
    if mark_used:
        mark_worktree_used(result.path)
    return result


def resolve_routed_host_worktree_cwd(
    group_folder: str,
    source_cwd: Path,
    repo_contexts: Sequence[RepoContext],
    *,
    recovered: bool,
) -> RoutedHostWorktreeResult:
    """Resolve a routed host run through this module's worktree provisioner."""
    return _routed_host_worktree.resolve_routed_host_worktree_cwd(
        group_folder,
        source_cwd,
        repo_contexts,
        recovered=recovered,
        ensure_worktree_fn=ensure_worktree,
    )


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
        merge = run_git(
            "-c",
            "user.name=Pynchy",
            "-c",
            "user.email=pynchy@localhost",
            "merge",
            "--no-edit",
            f"origin/{main_branch}",
            cwd=worktree_path,
            env=git_env_without_credentials(include_identity=False),
            inherit_env=False,
        )
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
    env = git_env_with_token(repo_ctx.slug)
    # Fetch is required for initial creation
    fetch = run_git("fetch", "origin", cwd=repo_ctx.root, env=env)
    if fetch.returncode != 0:
        raise WorktreeError(_GIT_FETCH_FAILED.format(stderr=fetch.stderr.strip()))

    repo_ctx.worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Clean up stale worktree metadata before attaching or creating the branch.
    run_git("worktree", "prune", cwd=repo_ctx.root)
    branch = run_git(
        "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}", cwd=repo_ctx.root
    )
    if branch.returncode == 0:
        # A missing directory can retain committed routed work on its branch.
        # Reattach it instead of deleting the branch before recovery can resume.
        add = run_git(
            "worktree",
            "add",
            str(worktree_path),
            branch_name,
            cwd=repo_ctx.root,
        )
    elif branch.returncode == 1:
        add = run_git(
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path),
            f"origin/{main_branch}",
            cwd=repo_ctx.root,
        )
    else:
        raise WorktreeError(_GIT_WORKTREE_ADD_FAILED.format(stderr=branch.stderr.strip()))
    if add.returncode != 0:
        raise WorktreeError(_GIT_WORKTREE_ADD_FAILED.format(stderr=add.stderr.strip()))

    logger.info(
        "Worktree created",
        group=group_folder,
        branch=branch_name,
        path=str(worktree_path),
    )
    return WorktreeResult(path=worktree_path)


def _run_hook_installer(
    repo_root: Path,
    runner: str,
) -> subprocess.CompletedProcess[str]:
    args = ["uv", "tool", "run", runner, "install"]
    return subprocess.run(  # noqa: S603 - runner comes from a fixed config-to-runner table.
        args,  # uv is the trusted tool runner.
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def install_repo_hooks(repo_root: Path) -> None:
    """Install the repository's declared hook runner into its shared Git directory.

    Git worktrees share hooks from the main repository, so one installation
    covers every agent workspace.
    """
    runner = next(
        (
            candidate_runner
            for config_name, candidate_runner in (("prek.toml", "prek"),)
            if (repo_root / config_name).exists()
        ),
        None,
    )
    if runner is None:
        return

    try:
        result = _run_hook_installer(repo_root, runner)
        if result.returncode != 0:
            logger.warning(
                "Repository hook install failed (workspace unaffected)",
                repo=str(repo_root),
                runner=runner,
                stderr=result.stderr.strip(),
            )
            return
        logger.info("Repository hooks installed", repo=str(repo_root), runner=runner)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "Repository hook install error (workspace unaffected)",
            repo=str(repo_root),
            runner=runner,
            err=str(exc),
        )


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
    repo_groups = repo_groups or {}

    runtime = _runtime
    for slug, folders in repo_groups.items():
        repo_ctx = _startup_repo_context(slug, repo_manager.get_repo_context)
        if repo_ctx is None:
            continue
        _warn_if_repo_token_missing(
            slug,
            runtime.configured_tokens.get(slug),
            repo_manager.check_token_expiry,
            repo_manager.get_repo_token,
        )
        if not _prepare_repo_for_startup(
            repo_ctx,
            slug,
            repo_manager.ensure_repo_cloned,
        ):
            continue
        _ensure_startup_worktrees(slug, folders, repo_ctx)
        _rebase_diverged_worktrees(repo_ctx)


def _startup_repo_context(
    slug: str,
    get_repo_context: Callable[[str], RepoContext | None],
) -> RepoContext | None:
    repo_ctx = get_repo_context(slug)
    if repo_ctx is None:
        logger.warning("Slug not configured in [repos], skipping", slug=slug)
    return repo_ctx


def _warn_if_repo_token_missing(
    slug: str,
    configured_token: str | None,
    check_token_expiry: Callable[[str, str], None],
    get_repo_token: Callable[[str], str | None],
) -> None:
    if configured_token is not None:
        check_token_expiry(slug, configured_token)
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
    ensure_repo_cloned: Callable[[RepoContext], bool],
) -> bool:
    if not ensure_repo_cloned(repo_ctx):
        logger.warning("Repo not available, skipping worktree reconciliation", slug=slug)
        return False

    run_git("worktree", "prune", cwd=repo_ctx.root)
    install_repo_hooks(repo_ctx.root)
    return True


def _ensure_startup_worktrees(slug: str, folders: list[str], repo_ctx: RepoContext) -> None:
    for folder in folders:
        try:
            ensure_worktree(folder, repo_ctx, mark_used=False)
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
