"""Git sync drift helpers used by Temporal activities."""

from __future__ import annotations

import asyncio
import subprocess  # noqa: S404 - used only for subprocess exception types.
from collections.abc import (
    Callable,
)
from dataclasses import dataclass
from pathlib import Path

from pynchy.host.git_ops._worktree_notify import host_notify_worktree_updates, last_notified_sha
from pynchy.host.git_ops.repo import (
    RepoContext,  # beartype resolves git sync helpers at runtime.
    get_repo_context,
)
from pynchy.host.git_ops.utils import (
    detect_main_branch,
    files_changed_between,
    get_head_sha,
    push_local_commits,
    redact_git_diagnostic,
    run_git,
)
from pynchy.host.git_ops.worktree_sync import (
    GitSyncDeps,
)
from pynchy.host.orchestrator.scheduler_deps import (
    HostSyncState,
)
from pynchy.logger import logger


@dataclass(frozen=True)
class GitOriginProbe:
    """A remote-main lookup that retains a safe failure diagnostic."""

    sha: str | None
    error: str | None = None


@dataclass(frozen=True)
class GitUpdateResult:
    """Outcome of updating a local main checkout from its origin."""

    succeeded: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GitSyncRuntime:
    """Resolved host paths and repository identities for sync polling."""

    project_root: Path
    repo_slugs: tuple[str, ...]
    get_restart_hash: Callable[[], str]


_git_sync_runtime: GitSyncRuntime | None = None


def configure_git_sync_runtime(runtime: GitSyncRuntime) -> None:
    """Install host sync inputs at application composition."""
    global _git_sync_runtime  # noqa: PLW0603 - one host process owns sync configuration.
    _git_sync_runtime = runtime


def _configured_git_sync_runtime() -> GitSyncRuntime:
    if _git_sync_runtime is None:
        raise RuntimeError("git sync runtime has not been configured")
    return _git_sync_runtime


def get_local_head_sha(repo_root: Path | None = None) -> str:
    """Get the local HEAD SHA."""
    sha = get_head_sha(cwd=repo_root)
    return "" if sha == "unknown" else sha


def probe_origin_main_sha(repo_root: Path, env: dict[str, str] | None = None) -> GitOriginProbe:
    """Look up origin/main and retain only a redacted bounded diagnostic."""
    main = "main"
    try:
        main = detect_main_branch(cwd=repo_root)
        result = run_git("ls-remote", "origin", f"refs/heads/{main}", cwd=repo_root, env=env)
        if result.returncode == 0 and result.stdout.strip():
            return GitOriginProbe(sha=result.stdout.strip().split()[0])
    except (subprocess.TimeoutExpired, OSError) as exc:
        diagnostic = redact_git_diagnostic(
            str(exc),
            token=env.get("GH_TOKEN") if env else None,
        )
        return GitOriginProbe(
            sha=None,
            error=f"git ls-remote origin refs/heads/{main} failed: {diagnostic}",
        )

    diagnostic = redact_git_diagnostic(
        result.stderr or "",
        token=env.get("GH_TOKEN") if env else None,
    )
    if result.returncode == 0:
        return GitOriginProbe(
            sha=None,
            error=f"git ls-remote origin refs/heads/{main} returned no revision",
        )
    suffix = f": {diagnostic}" if diagnostic else ""
    return GitOriginProbe(
        sha=None,
        error=(
            f"git ls-remote origin refs/heads/{main} failed with exit {result.returncode}{suffix}"
        ),
    )


def host_get_origin_main_sha(repo_root: Path, env: dict[str, str] | None = None) -> str | None:
    """Return only the SHA from :func:`probe_origin_main_sha`."""
    return probe_origin_main_sha(repo_root, env).sha


def host_update_main_result(repo_root: Path, env: dict[str, str] | None = None) -> GitUpdateResult:
    """Fetch origin and rebase main, retaining a redacted failure diagnostic.

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
        diagnostic = redact_git_diagnostic(
            fetch.stderr or "",
            token=env.get("GH_TOKEN") if env else None,
        )
        error = f"git fetch origin failed with exit {fetch.returncode}"
        if diagnostic:
            error = f"{error}: {diagnostic}"
        logger.warning("git_sync poll: fetch failed", error=error)
        return GitUpdateResult(succeeded=False, error=error)

    main_branch = detect_main_branch(cwd=repo_root)
    rebase = run_git("rebase", f"origin/{main_branch}", cwd=repo_root)
    if rebase.returncode != 0:
        run_git("rebase", "--abort", cwd=repo_root)
        diagnostic = redact_git_diagnostic(rebase.stderr or "")
        error = f"git rebase origin/{main_branch} failed with exit {rebase.returncode}"
        if diagnostic:
            error = f"{error}: {diagnostic}"
        logger.warning("git_sync poll: rebase failed", error=error)
        return GitUpdateResult(succeeded=False, error=error)

    # --- Push any rebased local commits ---
    push_local_commits(skip_fetch=True, cwd=repo_root, env=env)

    # --- Restore stashed work ---
    if stashed:
        pop = run_git("stash", "pop", cwd=repo_root)
        if pop.returncode != 0:
            # A failed pop retains the stash; never restart from conflict-marked source.
            diagnostic = redact_git_diagnostic(pop.stderr or "")
            error = f"git stash pop failed with exit {pop.returncode}"
            if diagnostic:
                error = f"{error}: {diagnostic}"
            logger.warning(
                "git_sync poll: stash pop conflict, work in stash/reflog",
                error=error,
                recovery="stash-pop-conflict",
            )
            return GitUpdateResult(succeeded=False, error=error)

    return GitUpdateResult(succeeded=True)


def host_update_main(repo_root: Path, env: dict[str, str] | None = None) -> bool:
    """Fetch origin and rebase main onto origin/main. Returns True on success."""
    return host_update_main_result(repo_root, env).succeeded


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


def get_deploy_config_hash() -> str:
    """Return the effective hash of host configuration that requires restart."""
    return _configured_git_sync_runtime().get_restart_hash()


def _find_pynchy_repo_ctx(
    repo_slugs: tuple[str, ...],
    pynchy_root: Path,
) -> RepoContext | None:
    """Resolve pynchy's own RepoContext (for worktree notifications), if configured."""
    for slug in repo_slugs:
        ctx = get_repo_context(slug)
        if ctx and ctx.root.resolve() == pynchy_root.resolve():
            return ctx
    return None


async def _check_local_head_drift(
    pynchy_root: Path,
    state: HostSyncState,
    pynchy_repo_ctx: RepoContext | None,
    deps: GitSyncDeps,
    *,
    auto_deploy: bool,
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
    if not auto_deploy:
        await _offer_update_if_needed(state, deps, state.local_head)
        return False
    if pynchy_repo_ctx:
        notified = last_notified_sha.get(str(pynchy_root), "")
        if notified != state.local_head:
            await host_notify_worktree_updates(None, deps, pynchy_repo_ctx)
    rebuild = needs_container_rebuild(state.deployed_sha, state.local_head)
    await deps.trigger_deploy(state.deployed_sha, rebuild=rebuild)
    return True


async def _offer_update_if_needed(
    state: HostSyncState,
    deps: GitSyncDeps,
    commit_sha: str,
) -> bool:
    """Notify the admin once for each revision that awaits approval."""
    if state.offered_sha == commit_sha:
        return True
    offer_update = getattr(deps, "offer_update", None)
    if not callable(offer_update):
        logger.error("Git sync cannot offer a pending update", commit_sha=commit_sha)
        return False
    try:
        offered = await offer_update(commit_sha)
    # allow: exception-handling - a notification failure must be retried by the next poll.
    except Exception as exc:  # noqa: BLE001
        logger.warning("Git sync update offer failed", commit_sha=commit_sha, error=str(exc))
        return False
    if offered is False:
        return False
    state.offered_sha = commit_sha
    return True


async def check_origin_drift(
    pynchy_root: Path,
    state: HostSyncState,
    pynchy_repo_ctx: RepoContext | None,
    deps: GitSyncDeps,
    *,
    auto_deploy: bool,
) -> bool:
    """Detect origin/main drift, pull, and deploy if needed. Returns True to stop the loop."""
    current_origin = await asyncio.to_thread(host_get_origin_main_sha, pynchy_root)
    if not current_origin or current_origin == state.last_origin_sha:
        return False

    _log_origin_sync(state.last_origin_sha, current_origin)

    if not auto_deploy:
        # Keep the running checkout intact until an admin approves the
        # advertised revision while retaining a cheap remote-SHA poll.
        if await _offer_update_if_needed(state, deps, current_origin):
            state.last_origin_sha = current_origin
        return False

    if state.local_head == current_origin:
        state.last_origin_sha = current_origin
        logger.info("Origin changed but local already matches, skipping pull")
        return False  # drift check above already handled deploy

    updated = await asyncio.to_thread(host_update_main, pynchy_root)
    if not updated:
        return False
    state.last_origin_sha = current_origin

    new_head = await asyncio.to_thread(get_local_head_sha, pynchy_root)
    await _notify_origin_sync_if_needed(pynchy_root, pynchy_repo_ctx, deps, new_head)
    return await _deploy_pulled_head_if_needed(state, deps, new_head)


def _log_origin_sync(old_origin: str | None, current_origin: str) -> None:
    logger.info(
        "Origin/main changed, syncing",
        old_sha=old_origin[:8] if old_origin else "none",
        new_sha=current_origin[:8],
    )


async def _notify_origin_sync_if_needed(
    pynchy_root: Path,
    pynchy_repo_ctx: RepoContext | None,
    deps: GitSyncDeps,
    new_head: str,
) -> None:
    if pynchy_repo_ctx is None:
        return
    notified = last_notified_sha.get(str(pynchy_root), "")
    if notified == new_head:
        return
    await host_notify_worktree_updates(None, deps, pynchy_repo_ctx)


async def _deploy_pulled_head_if_needed(
    state: HostSyncState,
    deps: GitSyncDeps,
    new_head: str,
) -> bool:
    # Check deploy inline (avoid 5s delay for next tick)
    if state.deployed_sha and new_head and needs_deploy(state.deployed_sha, new_head):
        rebuild = needs_container_rebuild(state.deployed_sha, new_head)
        await deps.trigger_deploy(state.deployed_sha, rebuild=rebuild)
        return True
    state.deployed_sha = new_head
    return False
