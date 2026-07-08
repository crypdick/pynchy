"""Git sync drift helpers used by Temporal activities."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from pynchy.config import get_settings
from pynchy.config.settings import Settings
from pynchy.host.git_ops._worktree_notify import host_notify_worktree_updates, last_notified_sha
from pynchy.host.git_ops.repo import RepoContext
from pynchy.host.git_ops.sync import GitSyncDeps
from pynchy.host.git_ops.utils import (
    detect_main_branch,
    files_changed_between,
    get_head_sha,
    push_local_commits,
    run_git,
)
from pynchy.logger import logger


def get_local_head_sha(repo_root: Path | None = None) -> str:
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


def host_update_main(repo_root: Path, env: dict[str, str] | None = None) -> bool:
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
    """Check if agent-side files changed between two commits."""
    return files_changed_between(old_sha, new_sha, "src/pynchy/agent/")


def host_source_files_changed(old_sha: str, new_sha: str) -> bool:
    """Check if host source files changed between two commits.

    The running Python process holds stale modules in memory. A restart is needed
    to pick up src/ changes — git pull alone doesn't hot-reload Python.
    """
    return files_changed_between(old_sha, new_sha, "src/")


def needs_deploy(old_sha: str, new_sha: str) -> bool:
    """Check if a restart is needed between two commits."""
    return _host_container_files_changed(old_sha, new_sha) or host_source_files_changed(
        old_sha, new_sha
    )


def needs_container_rebuild(old_sha: str, new_sha: str) -> bool:
    """Check if container image needs rebuilding. Only src/pynchy/agent/ changes require this."""
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


def _find_pynchy_repo_ctx(s: Settings, pynchy_root: Path) -> RepoContext | None:
    """Resolve pynchy's own RepoContext (for worktree notifications), if configured."""
    from pynchy.host.git_ops.repo import get_repo_context

    for slug in s.repos:
        ctx = get_repo_context(slug)
        if ctx and ctx.root.resolve() == pynchy_root.resolve():
            return ctx
    return None


@dataclass
class _HostSyncState:
    """Mutable baseline tracked across polling iterations."""

    last_origin_sha: str | None
    deployed_sha: str
    config_hash: str
    local_head: str | None = None


async def _check_config_drift(state: _HostSyncState, deps: GitSyncDeps) -> bool:
    """Return True (after triggering a deploy) if config files drifted."""
    current_config_hash = _hash_config_files()
    if current_config_hash == state.config_hash:
        return False
    logger.info("Config files changed, triggering restart")
    await deps.trigger_deploy(state.deployed_sha, rebuild=False)
    return True


async def _check_local_head_drift(
    pynchy_root: Path,
    state: _HostSyncState,
    pynchy_repo_ctx: RepoContext | None,
    deps: GitSyncDeps,
) -> bool:
    """Detect local HEAD drift and deploy if needed. Returns True to stop the loop."""
    state.local_head = await asyncio.to_thread(get_local_head_sha, pynchy_root)
    if not (state.local_head and state.deployed_sha and state.local_head != state.deployed_sha):
        return False

    if not needs_deploy(state.deployed_sha, state.local_head):
        state.deployed_sha = state.local_head  # no deploy-worthy changes, advance baseline
        return False

    logger.info(
        "Local HEAD drifted, deploy needed",
        deployed_sha=state.deployed_sha[:8],
        local_head=state.local_head[:8],
    )
    if pynchy_repo_ctx:
        notified = last_notified_sha.get(str(pynchy_root), "")
        if notified != state.local_head:
            await host_notify_worktree_updates(None, deps, pynchy_repo_ctx)
    rebuild = needs_container_rebuild(state.deployed_sha, state.local_head)
    await deps.trigger_deploy(state.deployed_sha, rebuild=rebuild)
    return True


async def _check_origin_drift(
    pynchy_root: Path,
    state: _HostSyncState,
    pynchy_repo_ctx: RepoContext | None,
    deps: GitSyncDeps,
) -> bool:
    """Detect origin/main drift, pull, and deploy if needed. Returns True to stop the loop."""
    current_origin = await asyncio.to_thread(_host_get_origin_main_sha, pynchy_root)
    if not current_origin or current_origin == state.last_origin_sha:
        return False

    old_origin = state.last_origin_sha
    logger.info(
        "Origin/main changed, syncing",
        old_sha=old_origin[:8] if old_origin else "none",
        new_sha=current_origin[:8],
    )

    if state.local_head == current_origin:
        state.last_origin_sha = current_origin
        logger.info("Origin changed but local already matches, skipping pull")
        return False  # drift check above already handled deploy

    updated = await asyncio.to_thread(host_update_main, pynchy_root)
    if not updated:
        return False
    state.last_origin_sha = current_origin

    new_head_after_pull = await asyncio.to_thread(get_local_head_sha, pynchy_root)
    if pynchy_repo_ctx:
        notified = last_notified_sha.get(str(pynchy_root), "")
        if notified != new_head_after_pull:
            await host_notify_worktree_updates(None, deps, pynchy_repo_ctx)

    # Check deploy inline (avoid 5s delay for next tick)
    new_head = await asyncio.to_thread(get_local_head_sha, pynchy_root)
    if state.deployed_sha and new_head and needs_deploy(state.deployed_sha, new_head):
        rebuild = needs_container_rebuild(state.deployed_sha, new_head)
        await deps.trigger_deploy(state.deployed_sha, rebuild=rebuild)
        return True
    state.deployed_sha = new_head
    return False
