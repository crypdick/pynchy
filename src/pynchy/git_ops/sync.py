"""Coordinated git sync between host and container worktrees.

Host owns main — agents never push to main directly. The host mediates
all merges into main, pushes to origin, and syncs other running agents.

Container-side errors must be self-contained and actionable since
containers can't read host state (logs, config, etc.).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Protocol

from pynchy.config import get_settings
from pynchy.git_ops.repo import RepoContext
from pynchy.git_ops.utils import (
    detect_main_branch,
    files_changed_between,
    get_head_sha,
    push_local_commits,
    run_git,
)
from pynchy.logger import logger
from pynchy.types import WorkspaceProfile

# Authenticated GitHub API rate limit: 5000 req/hr (83/min).
# git ls-remote every 5s = 720 req/hr — well within limits.
HOST_GIT_SYNC_POLL_INTERVAL = 5.0

# Valid git_policy values
GIT_POLICY_MERGE = "merge-to-main"
GIT_POLICY_PR = "pull-request"

# Track the last HEAD SHA for which worktree notifications were sent, per repo root.
# This prevents the poll loop from re-notifying when the IPC handler
# (sync_worktree_to_main) already notified for the same merge.
_last_worktree_notified_sha: dict[str, str] = {}


class GitSyncDeps(Protocol):
    """Dependencies for the git sync loop."""

    async def broadcast_host_message(self, jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, jid: str, text: str) -> None: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None: ...


# ---------------------------------------------------------------------------
# host_sync_worktree — merge a single worktree into main and push
# ---------------------------------------------------------------------------


def host_sync_worktree(group_folder: str, repo_ctx: RepoContext) -> dict[str, Any]:
    """Host-side: merge a worktree into main and push to origin.

    Container can't read host state — all feedback must be in the response.
    On conflict, leaves the worktree with conflict markers so the agent
    can fix them without leaving the container.

    Returns {"success": bool, "message": str}.
    """
    worktree_path = repo_ctx.worktrees_dir / group_folder
    branch_name = f"worktree/{group_folder}"
    main_branch = detect_main_branch(cwd=repo_ctx.root)

    if not worktree_path.exists():
        return {
            "success": False,
            "message": f"No worktree found for {group_folder}. Nothing to sync.",
        }

    # 1. Check for uncommitted changes
    status = run_git("status", "--porcelain", cwd=worktree_path)
    if status.returncode == 0 and status.stdout.strip():
        return {
            "success": False,
            "message": (
                "You have uncommitted changes. Commit all changes first, "
                "then call sync_worktree_to_main again.\n"
                "Run `git status` to see uncommitted files."
            ),
        }

    # 2. Check if there are commits to merge
    count = run_git("rev-list", f"{main_branch}..{branch_name}", "--count", cwd=repo_ctx.root)
    if count.returncode != 0:
        return {
            "success": False,
            "message": (
                f"Failed to check commits: {count.stderr.strip()}. "
                "Verify your branch is valid with `git log --oneline`."
            ),
        }
    try:
        ahead = int(count.stdout.strip())
    except (ValueError, TypeError):
        return {
            "success": False,
            "message": (
                f"Failed to parse commit count: {count.stdout.strip()!r}. "
                "Verify your branch is valid with `git log --oneline`."
            ),
        }
    if ahead == 0:
        return {
            "success": True,
            "message": "Already up to date — no commits to merge into main.",
        }

    # 3. Fetch origin
    fetch = run_git("fetch", "origin", cwd=repo_ctx.root)
    if fetch.returncode != 0:
        return {
            "success": False,
            "message": (
                f"git fetch failed: {fetch.stderr.strip()}. "
                "Check network connectivity and try again."
            ),
        }

    # 4. Rebase host main onto origin/main (catch up with remote)
    rebase_main = run_git("rebase", f"origin/{main_branch}", cwd=repo_ctx.root)
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

    # 5. Rebase worktree onto main (from within the worktree)
    rebase_wt = run_git("rebase", main_branch, cwd=worktree_path)
    if rebase_wt.returncode != 0:
        # Leave conflict markers for agent to resolve
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

    # 6. FF-merge worktree branch into main
    merge = run_git("merge", "--ff-only", branch_name, cwd=repo_ctx.root)
    if merge.returncode != 0:
        return {
            "success": False,
            "message": (
                f"Fast-forward merge failed: {merge.stderr.strip()}. "
                "This is unexpected after a successful rebase. "
                "Try running `git log --oneline --graph` to inspect the state."
            ),
        }

    # 7. Push to origin (skip_fetch since we just fetched)
    pushed = push_local_commits(skip_fetch=True, cwd=repo_ctx.root)
    if not pushed:
        return {
            "success": False,
            "message": (
                "Merge succeeded but push to origin failed. "
                "Your commits are on the host's main branch. "
                "The host will retry pushing automatically."
            ),
        }

    logger.info(
        "Worktree synced to main and pushed",
        group=group_folder,
        commits=ahead,
    )
    return {
        "success": True,
        "message": f"Merged {ahead} commit(s) into main and pushed to origin.",
    }


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------


def resolve_git_policy(group_folder: str) -> str:
    """Resolve the effective git policy for a workspace.

    Returns "merge-to-main" (default) or "pull-request".
    """
    s = get_settings()
    ws_cfg = s.workspaces.get(group_folder)
    if ws_cfg and ws_cfg.git_policy:
        return ws_cfg.git_policy
    return GIT_POLICY_MERGE


# ---------------------------------------------------------------------------
# host_create_pr_from_worktree — push branch and open/update a PR
# ---------------------------------------------------------------------------


def host_create_pr_from_worktree(
    group_folder: str,
    repo_ctx: RepoContext,
) -> dict[str, Any]:
    """Host-side: push worktree branch to origin and open/update a PR.

    Idempotent: if a PR already exists for the branch, just pushes (PR
    auto-updates). No duplicate PRs.

    Returns {"success": bool, "message": str}.
    """
    worktree_path = repo_ctx.worktrees_dir / group_folder
    branch_name = f"worktree/{group_folder}"
    main_branch = detect_main_branch(cwd=repo_ctx.root)

    if not worktree_path.exists():
        return {
            "success": False,
            "message": f"No worktree found for {group_folder}.",
        }

    # 1. Check for uncommitted changes
    status = run_git("status", "--porcelain", cwd=worktree_path)
    if status.returncode == 0 and status.stdout.strip():
        return {
            "success": False,
            "message": (
                "You have uncommitted changes. Commit all changes first, "
                "then call sync_worktree_to_main again."
            ),
        }

    # 2. Check if there are commits ahead of main
    count = run_git("rev-list", f"{main_branch}..{branch_name}", "--count", cwd=repo_ctx.root)
    try:
        ahead = int(count.stdout.strip())
    except (ValueError, TypeError):
        return {
            "success": False,
            "message": f"Failed to check commits: {count.stderr.strip()}",
        }
    if ahead == 0:
        return {
            "success": True,
            "message": "Already up to date — no commits to push.",
        }

    # 3. Push the worktree branch to origin
    push = run_git("push", "-u", "origin", branch_name, "--force-with-lease", cwd=repo_ctx.root)
    if push.returncode != 0:
        return {
            "success": False,
            "message": f"Push failed: {push.stderr.strip()}",
        }

    # 4. Check if a PR already exists for this branch
    pr_check = subprocess.run(
        ["gh", "pr", "view", branch_name, "--json", "url", "--jq", ".url"],
        cwd=str(repo_ctx.root),
        capture_output=True,
        text=True,
        timeout=30,
    )

    if pr_check.returncode == 0 and pr_check.stdout.strip():
        pr_url = pr_check.stdout.strip()
        return {
            "success": True,
            "message": f"Pushed {ahead} commit(s) to {branch_name}. PR updated: {pr_url}",
        }

    # 5. Create a new PR
    title_result = run_git("log", "-1", "--format=%s", cwd=worktree_path)
    pr_title = (
        title_result.stdout.strip()
        if title_result.returncode == 0
        else f"Changes from {group_folder}"
    )

    body_result = run_git(
        "log",
        f"{main_branch}..{branch_name}",
        "--format=- %s",
        cwd=repo_ctx.root,
    )
    pr_body = (
        f"Automated PR from workspace `{group_folder}`.\n\n"
        f"### Commits\n{body_result.stdout.strip()}"
    )

    pr_create = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            main_branch,
            "--head",
            branch_name,
            "--title",
            pr_title,
            "--body",
            pr_body,
        ],
        cwd=str(repo_ctx.root),
        capture_output=True,
        text=True,
        timeout=30,
    )

    if pr_create.returncode != 0:
        return {
            "success": False,
            "message": (
                f"Pushed {ahead} commit(s) to {branch_name}, but PR creation failed: "
                f"{pr_create.stderr.strip()}"
            ),
        }

    pr_url = pr_create.stdout.strip()
    logger.info(
        "Worktree pushed and PR created",
        group=group_folder,
        commits=ahead,
        pr_url=pr_url,
    )
    return {
        "success": True,
        "message": f"Pushed {ahead} commit(s) and opened PR: {pr_url}",
    }


# ---------------------------------------------------------------------------
# Notification formatting
# ---------------------------------------------------------------------------


def _build_rebase_notice(worktree_path: Path, old_head: str, commit_count: int) -> str:
    """Build a descriptive auto-rebase notification for an agent.

    Shows commit count, files changed, and — for single commits — the full
    commit message so the agent understands what landed without extra commands.
    """
    parts = [f"Auto-rebased {commit_count} commit(s) onto your worktree."]

    # File change stats (e.g. "3 files changed, 42 insertions(+), 10 deletions(-)")
    diffstat = run_git("diff", "--stat", old_head, "HEAD", cwd=worktree_path)
    if diffstat.returncode == 0 and diffstat.stdout.strip():
        # Last line of --stat is the summary (e.g. "3 files changed, ...")
        stat_lines = diffstat.stdout.strip().splitlines()
        if stat_lines:
            parts.append(stat_lines[-1].strip())

    if commit_count == 1:
        # Show full commit message for single commits
        msg = run_git("log", "-1", "--format=%B", cwd=worktree_path)
        if msg.returncode == 0 and msg.stdout.strip():
            parts.append(f"Commit: {msg.stdout.strip()}")
    else:
        parts.append("Run `git log --oneline -5` to see what changed.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# host_notify_worktree_updates — rebase all worktrees and notify agents
# ---------------------------------------------------------------------------


async def host_notify_worktree_updates(
    exclude_group: str | None,
    deps: GitSyncDeps,
    repo_ctx: RepoContext,
) -> None:
    """Host-side: rebase all worktrees for a repo onto main, notify agents.

    For each worktree (excluding source):
    - Up to date: no notification
    - Clean + rebase succeeds: notify "auto-rebased, run git log to see changes"
    - Clean + rebase fails: DON'T abort — notify "conflicts, run git status to fix"
    - Dirty (uncommitted): skip rebase, notify "commit or stash, then sync"

    Always uses system_notice so the LLM sees the notification as a pseudo
    system message (the Anthropic SDK doesn't support injecting system messages,
    so we store them as user messages with a [System Notice] prefix).
    """
    global _last_worktree_notified_sha

    if not repo_ctx.worktrees_dir.exists():
        return

    main_branch = detect_main_branch(cwd=repo_ctx.root)
    registered = deps.workspaces()

    # Build folder->jid lookup
    folder_to_jid: dict[str, str] = {g.folder: jid for jid, g in registered.items()}

    for entry in sorted(repo_ctx.worktrees_dir.iterdir()):
        if not entry.is_dir():
            continue

        group_folder = entry.name
        if group_folder == exclude_group:
            continue

        jid = folder_to_jid.get(group_folder)
        if not jid:
            continue

        # Check if behind main
        branch_name = f"worktree/{group_folder}"
        behind = run_git("rev-list", f"{branch_name}..{main_branch}", "--count", cwd=repo_ctx.root)
        try:
            behind_n = int(behind.stdout.strip())
        except (ValueError, TypeError):
            behind_n = 0
        if behind.returncode != 0 or behind_n == 0:
            continue  # up to date or can't check

        notify = deps.broadcast_system_notice

        # Check for uncommitted changes
        status = run_git("status", "--porcelain", cwd=entry)
        if status.returncode == 0 and status.stdout.strip():
            notice = (
                "Main branch has been updated, but your worktree has "
                "uncommitted changes. Commit or stash your work, then call "
                "sync_worktree_to_main to get the latest changes."
            )
            await notify(jid, notice)
            logger.info(
                "Skipped dirty worktree rebase, notified agent",
                group=group_folder,
            )
            continue

        # Gather stats before rebase for the notification
        behind_count = behind_n
        head_before = run_git("rev-parse", "HEAD", cwd=entry).stdout.strip()

        # Attempt rebase
        rebase = run_git("rebase", main_branch, cwd=entry)
        if rebase.returncode != 0:
            # Leave conflict markers for agent to resolve
            notice = (
                "Main branch was updated but your worktree has "
                "rebase conflicts. Run `git status` to see conflicted files, "
                "resolve them, then `git add` and `git rebase --continue`."
            )
            await notify(jid, notice)
            logger.warning(
                "Worktree rebase conflict during broadcast",
                group=group_folder,
                error=rebase.stderr.strip(),
            )
        else:
            notice = _build_rebase_notice(entry, head_before, behind_count)
            await notify(jid, notice)
            logger.info("Auto-rebased worktree", group=group_folder)

    # Record current HEAD so the poll loop can skip duplicate notifications
    # for the same merge (e.g. IPC handler already notified, poll loop detects
    # the same HEAD change seconds later).
    current_head = get_head_sha(cwd=repo_ctx.root)
    if current_head != "unknown":
        _last_worktree_notified_sha[str(repo_ctx.root)] = current_head


# ---------------------------------------------------------------------------
# IPC response helper
# ---------------------------------------------------------------------------


def write_ipc_response(path: Path, data: dict[str, Any]) -> None:
    """Write an IPC response file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Polling loop helpers — pynchy's own repo
# ---------------------------------------------------------------------------


def _get_local_head_sha(repo_root: Path | None = None) -> str:
    """Get the local HEAD SHA."""
    sha = get_head_sha(cwd=repo_root)
    return "" if sha == "unknown" else sha


def _host_get_origin_main_sha(repo_root: Path) -> str | None:
    """Lightweight check: get origin/main SHA via ls-remote."""
    try:
        main = detect_main_branch(cwd=repo_root)
        result = run_git("ls-remote", "origin", f"refs/heads/{main}", cwd=repo_root)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Failed to get origin main SHA", err=str(exc))
    return None


def _host_update_main(repo_root: Path) -> bool:
    """Fetch origin and rebase main onto origin/main. Returns True on success.

    Includes pre-flight recovery for stale rebase state and dirty working trees
    left by crashed operations (interrupted rebase, killed process mid-merge).
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
    fetch = run_git("fetch", "origin", cwd=repo_root)
    if fetch.returncode != 0:
        logger.warning("git_sync poll: fetch failed", error=fetch.stderr.strip())
        return False

    main_branch = detect_main_branch(cwd=repo_root)
    rebase = run_git("rebase", f"origin/{main_branch}", cwd=repo_root)
    if rebase.returncode != 0:
        run_git("rebase", "--abort", cwd=repo_root)
        logger.warning("git_sync poll: rebase failed", error=rebase.stderr.strip())
        return False

    # --- Post-recovery: restore stashed work ---
    if stashed:
        run_git(
            "commit",
            "--allow-empty",
            "-m",
            "[pynchy-sync] dirty host repo recovered via stash \u2014 needs manual reconciliation",
            cwd=repo_root,
        )
        push_local_commits(skip_fetch=True, cwd=repo_root)
        pop = run_git("stash", "pop", cwd=repo_root)
        if pop.returncode != 0:
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
        Path(s.gateway.litellm_config) if s.gateway.litellm_config else None,
    ]:
        if path and path.exists():
            h.update(path.read_bytes())
        else:
            h.update(b"__missing__")
    return h.hexdigest()


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
                        notified = _last_worktree_notified_sha.get(str(pynchy_root), "")
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
                notified = _last_worktree_notified_sha.get(str(pynchy_root), "")
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
    """
    repo_root = repo_ctx.root
    last_origin_sha = await asyncio.to_thread(_host_get_origin_main_sha, repo_root)

    while True:
        await asyncio.sleep(HOST_GIT_SYNC_POLL_INTERVAL)

        try:
            current_origin = await asyncio.to_thread(_host_get_origin_main_sha, repo_root)
            if not current_origin or current_origin == last_origin_sha:
                continue

            old_origin = last_origin_sha

            logger.info(
                "External repo origin changed, syncing",
                slug=repo_ctx.slug,
                old_sha=old_origin[:8] if old_origin else "none",
                new_sha=current_origin[:8],
            )

            updated = await asyncio.to_thread(_host_update_main, repo_root)
            if not updated:
                continue
            last_origin_sha = current_origin

            new_head = await asyncio.to_thread(_get_local_head_sha, repo_root)
            notified = _last_worktree_notified_sha.get(str(repo_root), "")
            if notified != new_head:
                await host_notify_worktree_updates(None, deps, repo_ctx)

        except Exception:
            logger.exception("external_repo_sync poll error", slug=repo_ctx.slug)
