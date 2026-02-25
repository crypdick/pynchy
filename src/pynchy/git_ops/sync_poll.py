"""Background polling loops for git sync.

Polls for code and config changes, triggering deploys when needed.
Pynchy's own repo gets full deploy logic; external repos just sync
worktrees.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from pynchy.config import get_settings
from pynchy.git_ops._worktree_notify import host_notify_worktree_updates, last_notified_sha
from pynchy.git_ops.repo import RepoContext
from pynchy.git_ops.sync import GitSyncDeps
from pynchy.git_ops.utils import (
    detect_main_branch,
    files_changed_between,
    get_head_sha,
    git_env_with_token,
    push_local_commits,
    run_git,
)
from pynchy.logger import logger

# Authenticated GitHub API rate limit: 5000 req/hr (83/min).
# git ls-remote every 5s = 720 req/hr — well within limits.
HOST_GIT_SYNC_POLL_INTERVAL = 5.0


# ---------------------------------------------------------------------------
# Polling loop helpers
# ---------------------------------------------------------------------------


def _get_local_head_sha(repo_root: Path | None = None) -> str:
    """Get the local HEAD SHA."""
    sha = get_head_sha(cwd=repo_root)
    return "" if sha == "unknown" else sha


def _host_get_origin_main_sha(repo_root: Path, env: dict[str, str] | None = None) -> str | None:
    """Lightweight check: get origin/main SHA via ls-remote."""
    import subprocess

    try:
        main = detect_main_branch(cwd=repo_root)
        result = run_git("ls-remote", "origin", f"refs/heads/{main}", cwd=repo_root, env=env)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Failed to get origin main SHA", err=str(exc))
    return None


def _host_update_main(repo_root: Path, env: dict[str, str] | None = None) -> bool:
    """Fetch origin and rebase main onto origin/main. Returns True on success.

    Includes pre-flight recovery for stale rebase state and dirty working trees
    left by crashed operations (interrupted rebase, killed process mid-merge).

    Args:
        env: Optional environment for remote-facing git calls (fetch, push).
    """
    # --- Pre-flight: recover from stale state ---
    git_dir = repo_root / ".git"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        logger.warning("git_sync poll: aborting stale rebase", recovery="rebase-abort")
        run_git("rebase", "--abort", cwd=repo_root)

    stashed = False
    status = run_git("status", "--porcelain", cwd=repo_root)
    if status.returncode == 0 and status.stdout.strip():
        logger.warning(
            "git_sync poll: stashing dirty working tree",
            recovery="stash",
            files=status.stdout.strip().count("\n") + 1,
        )
        stash_result = run_git("stash", "--include-untracked", cwd=repo_root)
        stashed = stash_result.returncode == 0

    # --- Normal fetch + rebase ---
    fetch = run_git("fetch", "origin", cwd=repo_root, env=env)
    if fetch.returncode != 0:
        logger.warning("git_sync poll: fetch failed", error=fetch.stderr.strip())
        return False

    main_branch = detect_main_branch(cwd=repo_root)
    rebase = run_git("rebase", f"origin/{main_branch}", cwd=repo_root)
    if rebase.returncode != 0:
        run_git("rebase", "--abort", cwd=repo_root)
        logger.warning("git_sync poll: rebase failed", error=rebase.stderr.strip())
        return False

    # --- Push any rebased local commits ---
    push_local_commits(skip_fetch=True, cwd=repo_root, env=env)

    # --- Restore stashed work ---
    if stashed:
        pop = run_git("stash", "pop", cwd=repo_root)
        if pop.returncode != 0:
            # Stash pop failed (conflict) — create marker so the user knows
            # to reconcile manually.  The stashed work is still in the reflog.
            run_git(
                "commit",
                "--allow-empty",
                "-m",
                "[pynchy-sync] stash pop conflict after rebase"
                " \u2014 work preserved in stash/reflog",
                cwd=repo_root,
            )
            push_local_commits(skip_fetch=True, cwd=repo_root, env=env)
            logger.warning(
                "git_sync poll: stash pop conflict, work in stash/reflog",
                recovery="stash-pop-conflict",
            )

    return True


def _host_container_files_changed(old_sha: str, new_sha: str) -> bool:
    """Check if container/ files changed between two commits."""
    return files_changed_between(old_sha, new_sha, "container/")


def _host_source_files_changed(old_sha: str, new_sha: str) -> bool:
    """Check if host source files changed between two commits.

    The running Python process has old modules in memory. A restart is needed
    to pick up src/ changes — git pull alone doesn't hot-reload Python.
    """
    return files_changed_between(old_sha, new_sha, "src/")


def needs_deploy(old_sha: str, new_sha: str) -> bool:
    """Check if a restart is needed between two commits."""
    return _host_container_files_changed(old_sha, new_sha) or _host_source_files_changed(
        old_sha, new_sha
    )


def needs_container_rebuild(old_sha: str, new_sha: str) -> bool:
    """Check if container image needs rebuilding. Only container/ changes require this."""
    return _host_container_files_changed(old_sha, new_sha)


def _hash_config_files() -> str:
    """Hash config files that require a restart when changed."""
    h = hashlib.sha256()
    s = get_settings()
    for path in [
        s.project_root / "config.toml",
        s.project_root / ".env",
        Path(s.gateway.litellm_config) if s.gateway.litellm_config else None,
    ]:
        if path and path.exists():
            h.update(path.read_bytes())
        else:
            h.update(b"__missing__")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Polling loops
# ---------------------------------------------------------------------------


async def start_host_git_sync_loop(deps: GitSyncDeps) -> None:
    """Poll for code and config changes on pynchy's own repo.

    Detects origin drift, local drift, and config drift. Deploy logic only
    fires for pynchy — external repos use start_external_repo_sync_loop.
    """
    from pynchy.git_ops.repo import get_repo_context

    s = get_settings()
    pynchy_root = s.project_root

    # Resolve pynchy's RepoContext for worktree notifications
    pynchy_repo_ctx: RepoContext | None = None
    for slug in s.repos:
        ctx = get_repo_context(slug)
        if ctx and ctx.root.resolve() == pynchy_root.resolve():
            pynchy_repo_ctx = ctx
            break

    last_origin_sha = await asyncio.to_thread(_host_get_origin_main_sha, pynchy_root)
    deployed_sha = await asyncio.to_thread(_get_local_head_sha, pynchy_root)
    config_hash = _hash_config_files()

    while True:
        await asyncio.sleep(HOST_GIT_SYNC_POLL_INTERVAL)

        try:
            # --- Config file drift detection ---
            current_config_hash = _hash_config_files()
            if current_config_hash != config_hash:
                logger.info("Config files changed, triggering restart")
                await deps.trigger_deploy(deployed_sha, rebuild=False)
                return

            # --- Local HEAD drift detection ---
            local_head = await asyncio.to_thread(_get_local_head_sha, pynchy_root)
            if local_head and deployed_sha and local_head != deployed_sha:
                if needs_deploy(deployed_sha, local_head):
                    logger.info(
                        "Local HEAD drifted, deploy needed",
                        deployed_sha=deployed_sha[:8],
                        local_head=local_head[:8],
                    )
                    if pynchy_repo_ctx:
                        notified = last_notified_sha.get(str(pynchy_root), "")
                        if notified != local_head:
                            await host_notify_worktree_updates(None, deps, pynchy_repo_ctx)
                    rebuild = needs_container_rebuild(deployed_sha, local_head)
                    await deps.trigger_deploy(deployed_sha, rebuild=rebuild)
                    return
                deployed_sha = local_head  # no deploy-worthy changes, advance baseline

            # --- Origin change detection ---
            current_origin = await asyncio.to_thread(_host_get_origin_main_sha, pynchy_root)
            if not current_origin or current_origin == last_origin_sha:
                continue
            old_origin = last_origin_sha

            logger.info(
                "Origin/main changed, syncing",
                old_sha=old_origin[:8] if old_origin else "none",
                new_sha=current_origin[:8],
            )

            if local_head == current_origin:
                last_origin_sha = current_origin
                logger.info("Origin changed but local already matches, skipping pull")
                continue  # drift check above already handled deploy

            updated = await asyncio.to_thread(_host_update_main, pynchy_root)
            if not updated:
                continue
            last_origin_sha = current_origin

            new_head_after_pull = await asyncio.to_thread(_get_local_head_sha, pynchy_root)
            if pynchy_repo_ctx:
                notified = last_notified_sha.get(str(pynchy_root), "")
                if notified != new_head_after_pull:
                    await host_notify_worktree_updates(None, deps, pynchy_repo_ctx)

            # Check deploy inline (avoid 5s delay for next tick)
            new_head = await asyncio.to_thread(_get_local_head_sha, pynchy_root)
            if deployed_sha and new_head and needs_deploy(deployed_sha, new_head):
                rebuild = needs_container_rebuild(deployed_sha, new_head)
                await deps.trigger_deploy(deployed_sha, rebuild=rebuild)
                return
            deployed_sha = new_head

        except Exception:
            logger.exception("git_sync poll error")


async def start_external_repo_sync_loop(repo_ctx: RepoContext, deps: GitSyncDeps) -> None:
    """Poll for origin changes on an external (non-pynchy) repo.

    Simplified loop: no deploy logic. Polls ls-remote, fetches + rebases main,
    then notifies all worktrees for that repo. Shares HOST_GIT_SYNC_POLL_INTERVAL.
    Uses per-repo token for all remote git operations.
    """
    repo_root = repo_ctx.root
    env = git_env_with_token(repo_ctx.slug)
    last_origin_sha = await asyncio.to_thread(_host_get_origin_main_sha, repo_root, env)

    while True:
        await asyncio.sleep(HOST_GIT_SYNC_POLL_INTERVAL)

        try:
            current_origin = await asyncio.to_thread(_host_get_origin_main_sha, repo_root, env)
            if not current_origin or current_origin == last_origin_sha:
                continue

            old_origin = last_origin_sha

            logger.info(
                "External repo origin changed, syncing",
                slug=repo_ctx.slug,
                old_sha=old_origin[:8] if old_origin else "none",
                new_sha=current_origin[:8],
            )

            updated = await asyncio.to_thread(_host_update_main, repo_root, env)
            if not updated:
                continue
            last_origin_sha = current_origin

            new_head = await asyncio.to_thread(_get_local_head_sha, repo_root)
            notified = last_notified_sha.get(str(repo_root), "")
            if notified != new_head:
                await host_notify_worktree_updates(None, deps, repo_ctx)

        except Exception:
            logger.exception("external_repo_sync poll error", slug=repo_ctx.slug)
