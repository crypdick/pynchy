"""Coordinated git sync between host and container worktrees.

Host owns main — agents never push to main directly. The host mediates
all merges into main, pushes to origin, and syncs other running agents.

Container-side errors must be self-contained and actionable since
containers can't read host state (logs, config, etc.).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Protocol

from pynchy.config import PROJECT_ROOT, WORKTREES_DIR
from pynchy.http_server import _push_local_commits
from pynchy.logger import logger
from pynchy.worktree import _detect_main_branch, _run_git

# Authenticated GitHub API rate limit: 5000 req/hr (83/min).
# git ls-remote every 5s = 720 req/hr — well within limits.
HOST_GIT_SYNC_POLL_INTERVAL = 5.0


class GitSyncDeps(Protocol):
    """Dependencies for the git sync loop."""

    async def send_message(self, jid: str, text: str) -> None: ...

    def registered_groups(self) -> dict[str, Any]: ...

    async def trigger_deploy(self) -> None: ...


# ---------------------------------------------------------------------------
# host_sync_worktree — merge a single worktree into main and push
# ---------------------------------------------------------------------------


def host_sync_worktree(group_folder: str) -> dict[str, Any]:
    """Host-side: merge a worktree into main and push to origin.

    Container can't read host state — all feedback must be in the response.
    On conflict, leaves the worktree with conflict markers so the agent
    can fix them without leaving the container.

    Returns {"success": bool, "message": str}.
    """
    worktree_path = WORKTREES_DIR / group_folder
    branch_name = f"worktree/{group_folder}"
    main_branch = _detect_main_branch()

    if not worktree_path.exists():
        return {
            "success": False,
            "message": f"No worktree found for {group_folder}. Nothing to sync.",
        }

    # 1. Check for uncommitted changes
    status = _run_git("status", "--porcelain", cwd=worktree_path)
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
    count = _run_git("rev-list", f"{main_branch}..{branch_name}", "--count")
    if count.returncode != 0:
        return {
            "success": False,
            "message": (
                f"Failed to check commits: {count.stderr.strip()}. "
                "Verify your branch is valid with `git log --oneline`."
            ),
        }
    ahead = int(count.stdout.strip())
    if ahead == 0:
        return {
            "success": True,
            "message": "Already up to date — no commits to merge into main.",
        }

    # 3. Fetch origin
    fetch = _run_git("fetch", "origin")
    if fetch.returncode != 0:
        return {
            "success": False,
            "message": (
                f"git fetch failed: {fetch.stderr.strip()}. "
                "Check network connectivity and try again."
            ),
        }

    # 4. Rebase host main onto origin/main (catch up with remote)
    rebase_main = _run_git("rebase", f"origin/{main_branch}")
    if rebase_main.returncode != 0:
        _run_git("rebase", "--abort")
        return {
            "success": False,
            "message": (
                "Host main branch has conflicts with origin. "
                "This requires manual intervention on the host. "
                "Your worktree commits are preserved — try again later."
            ),
        }

    # 5. Rebase worktree onto main (from within the worktree)
    rebase_wt = _run_git("rebase", main_branch, cwd=worktree_path)
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
    merge = _run_git("merge", "--ff-only", branch_name)
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
    pushed = _push_local_commits(skip_fetch=True)
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
# host_notify_worktree_updates — rebase all worktrees and notify agents
# ---------------------------------------------------------------------------


async def host_notify_worktree_updates(
    exclude_group: str | None,
    deps: GitSyncDeps,
) -> None:
    """Host-side: rebase all worktrees onto main, notify agents.

    For each worktree (excluding source):
    - Up to date: no notification
    - Clean + rebase succeeds: notify "auto-rebased, run git log to see changes"
    - Clean + rebase fails: DON'T abort — notify "conflicts, run git status to fix"
    - Dirty (uncommitted): skip rebase, notify "commit or stash, then sync"
    """
    if not WORKTREES_DIR.exists():
        return

    main_branch = _detect_main_branch()
    registered = deps.registered_groups()

    # Build folder->jid lookup
    folder_to_jid: dict[str, str] = {g.folder: jid for jid, g in registered.items()}

    for entry in sorted(WORKTREES_DIR.iterdir()):
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
        behind = _run_git("rev-list", f"{branch_name}..{main_branch}", "--count")
        if behind.returncode != 0 or int(behind.stdout.strip()) == 0:
            continue  # up to date or can't check

        # Check for uncommitted changes
        status = _run_git("status", "--porcelain", cwd=entry)
        if status.returncode == 0 and status.stdout.strip():
            notice = (
                "[git-sync] Main branch has been updated, but your worktree has "
                "uncommitted changes. Commit or stash your work, then call "
                "sync_worktree_to_main to get the latest changes."
            )
            await deps.send_message(jid, notice)
            logger.info(
                "Skipped dirty worktree rebase, notified agent",
                group=group_folder,
            )
            continue

        # Attempt rebase
        rebase = _run_git("rebase", main_branch, cwd=entry)
        if rebase.returncode != 0:
            # Leave conflict markers for agent to resolve
            notice = (
                "[git-sync] Main branch was updated but your worktree has "
                "rebase conflicts. Run `git status` to see conflicted files, "
                "resolve them, then `git add` and `git rebase --continue`."
            )
            await deps.send_message(jid, notice)
            logger.warning(
                "Worktree rebase conflict during broadcast",
                group=group_folder,
                error=rebase.stderr.strip(),
            )
        else:
            notice = (
                "[git-sync] Your worktree has been auto-rebased onto the latest main. "
                "Run `git log --oneline -5` to see recent changes."
            )
            await deps.send_message(jid, notice)
            logger.info("Auto-rebased worktree", group=group_folder)


# ---------------------------------------------------------------------------
# IPC response helper
# ---------------------------------------------------------------------------


def write_ipc_response(path: Path, data: dict[str, Any]) -> None:
    """Write an IPC response file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    import json

    tmp.write_text(json.dumps(data))
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Polling loop — detect origin/main changes
# ---------------------------------------------------------------------------


def _host_get_origin_main_sha() -> str | None:
    """Lightweight check: get origin/main SHA via ls-remote."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "origin", "refs/heads/main"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _host_update_main() -> bool:
    """Fetch origin and rebase main onto origin/main. Returns True on success."""
    fetch = _run_git("fetch", "origin")
    if fetch.returncode != 0:
        logger.warning("git_sync poll: fetch failed", error=fetch.stderr.strip())
        return False

    main_branch = _detect_main_branch()
    rebase = _run_git("rebase", f"origin/{main_branch}")
    if rebase.returncode != 0:
        _run_git("rebase", "--abort")
        logger.warning("git_sync poll: rebase failed", error=rebase.stderr.strip())
        return False

    return True


def _host_container_files_changed(old_sha: str, new_sha: str) -> bool:
    """Check if container/ files changed between two commits."""
    diff = subprocess.run(
        ["git", "diff", "--name-only", old_sha, new_sha, "--", "container/"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    return bool(diff.stdout.strip()) if diff.returncode == 0 else False


async def start_host_git_sync_loop(deps: GitSyncDeps) -> None:
    """Poll origin/main for external changes. Syncs host + all worktrees."""
    last_sha = await asyncio.to_thread(_host_get_origin_main_sha)

    while True:
        await asyncio.sleep(HOST_GIT_SYNC_POLL_INTERVAL)

        try:
            current_sha = await asyncio.to_thread(_host_get_origin_main_sha)
            if not current_sha or current_sha == last_sha:
                continue

            old_sha = last_sha
            last_sha = current_sha

            logger.info(
                "Origin/main changed, syncing",
                old_sha=old_sha[:8] if old_sha else "none",
                new_sha=current_sha[:8],
            )

            updated = await asyncio.to_thread(_host_update_main)
            if not updated:
                continue

            # Check if container files changed — trigger rebuild + restart
            if old_sha and _host_container_files_changed(old_sha, current_sha):
                logger.info("Container files changed, triggering deploy")
                await deps.trigger_deploy()
                return  # process will restart

            # Sync all worktrees + notify agents
            await host_notify_worktree_updates(exclude_group=None, deps=deps)

        except Exception as exc:
            logger.error("git_sync poll error", err=str(exc))
