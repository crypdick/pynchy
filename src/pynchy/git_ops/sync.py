"""Coordinated git sync between host and container worktrees.

Host owns main — agents never push to main directly. The host mediates
all merges into main, pushes to origin, and syncs other running agents.

Container-side errors must be self-contained and actionable since
containers can't read host state (logs, config, etc.).
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any, Protocol

from pynchy.config import get_settings
from pynchy.git_ops.repo import RepoContext
from pynchy.git_ops.utils import (
    detect_main_branch,
    get_head_sha,
    git_env_with_token,
    push_local_commits,
    run_git,
)
from pynchy.logger import logger
from pynchy.types import WorkspaceProfile

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

    def has_active_session(self, group_folder: str) -> bool: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None: ...


# ---------------------------------------------------------------------------
# Shared precondition validation for worktree sync operations
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _WorktreeContext:
    """Validated context for worktree sync operations."""

    worktree_path: Path
    branch_name: str
    main_branch: str
    env: dict[str, str]
    ahead: int


def _validate_sync_preconditions(
    group_folder: str,
    repo_ctx: RepoContext,
) -> _WorktreeContext | dict[str, Any]:
    """Validate common preconditions for worktree sync operations.

    Checks: worktree exists, no uncommitted changes, has commits ahead of main.
    Returns _WorktreeContext on success, or {"success": ..., "message": ...}
    error dict on failure.
    """
    worktree_path = repo_ctx.worktrees_dir / group_folder
    branch_name = f"worktree/{group_folder}"
    main_branch = detect_main_branch(cwd=repo_ctx.root)
    env = git_env_with_token(repo_ctx.slug)

    if not worktree_path.exists():
        return {
            "success": False,
            "message": f"No worktree found for {group_folder}. Nothing to sync.",
        }

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
            "message": "Already up to date — no new commits.",
        }

    return _WorktreeContext(
        worktree_path=worktree_path,
        branch_name=branch_name,
        main_branch=main_branch,
        env=env,
        ahead=ahead,
    )


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
    ctx = _validate_sync_preconditions(group_folder, repo_ctx)
    if isinstance(ctx, dict):
        return ctx

    # 1. Fetch origin
    fetch = run_git("fetch", "origin", cwd=repo_ctx.root, env=ctx.env)
    if fetch.returncode != 0:
        return {
            "success": False,
            "message": (
                f"git fetch failed: {fetch.stderr.strip()}. "
                "Check network connectivity and try again."
            ),
        }

    # 2. Rebase host main onto origin/main (catch up with remote)
    rebase_main = run_git("rebase", f"origin/{ctx.main_branch}", cwd=repo_ctx.root)
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

    # 3. Rebase worktree onto main (from within the worktree)
    rebase_wt = run_git("rebase", ctx.main_branch, cwd=ctx.worktree_path)
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

    # 4. FF-merge worktree branch into main
    merge = run_git("merge", "--ff-only", ctx.branch_name, cwd=repo_ctx.root)
    if merge.returncode != 0:
        return {
            "success": False,
            "message": (
                f"Fast-forward merge failed: {merge.stderr.strip()}. "
                "This is unexpected after a successful rebase. "
                "Try running `git log --oneline --graph` to inspect the state."
            ),
        }

    # 5. Push to origin (skip_fetch since we just fetched)
    pushed = push_local_commits(skip_fetch=True, cwd=repo_ctx.root, env=ctx.env)
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
        commits=ctx.ahead,
    )
    return {
        "success": True,
        "message": f"Merged {ctx.ahead} commit(s) into main and pushed to origin.",
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
    ctx = _validate_sync_preconditions(group_folder, repo_ctx)
    if isinstance(ctx, dict):
        return ctx

    # 1. Push the worktree branch to origin
    push = run_git(
        "push",
        "-u",
        "origin",
        ctx.branch_name,
        "--force-with-lease",
        cwd=repo_ctx.root,
        env=ctx.env,
    )
    if push.returncode != 0:
        return {
            "success": False,
            "message": f"Push failed: {push.stderr.strip()}",
        }

    # 2. Check if a PR already exists for this branch
    # env includes GH_TOKEN which gh CLI respects
    pr_check = subprocess.run(
        ["gh", "pr", "view", ctx.branch_name, "--json", "url", "--jq", ".url"],
        cwd=str(repo_ctx.root),
        capture_output=True,
        text=True,
        timeout=30,
        env=ctx.env,
    )

    if pr_check.returncode == 0 and pr_check.stdout.strip():
        pr_url = pr_check.stdout.strip()
        return {
            "success": True,
            "message": f"Pushed {ctx.ahead} commit(s) to {ctx.branch_name}. PR updated: {pr_url}",
        }

    # 3. Create a new PR
    title_result = run_git("log", "-1", "--format=%s", cwd=ctx.worktree_path)
    pr_title = (
        title_result.stdout.strip()
        if title_result.returncode == 0
        else f"Changes from {group_folder}"
    )

    body_result = run_git(
        "log",
        f"{ctx.main_branch}..{ctx.branch_name}",
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
            ctx.main_branch,
            "--head",
            ctx.branch_name,
            "--title",
            pr_title,
            "--body",
            pr_body,
        ],
        cwd=str(repo_ctx.root),
        capture_output=True,
        text=True,
        timeout=30,
        env=ctx.env,
    )

    if pr_create.returncode != 0:
        return {
            "success": False,
            "message": (
                f"Pushed {ctx.ahead} commit(s) to {ctx.branch_name}, but PR creation failed: "
                f"{pr_create.stderr.strip()}"
            ),
        }

    pr_url = pr_create.stdout.strip()
    logger.info(
        "Worktree pushed and PR created",
        group=group_folder,
        commits=ctx.ahead,
        pr_url=pr_url,
    )
    return {
        "success": True,
        "message": f"Pushed {ctx.ahead} commit(s) and opened PR: {pr_url}",
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

    Notification routing depends on session state:
    - Active conversation (has message history, regardless of whether the
      container is currently running): system_notice → LLM sees it on next
      wake, so it can act on conflicts or review changes.
    - No conversation (session was cleared or never started — no message
      history): host_message → human sees it in the channel, but the LLM
      never does.

    This distinction matters because system_notices persist in the DB and
    become part of the conversation history. If a workspace has no ongoing
    conversation, rebase notices accumulate and pollute the start of the
    next session with irrelevant "main was updated 5 times" spam. The agent
    gets current worktree state from ephemeral system_notices in
    agent_runner.py at container launch — those are always fresh. Persistent
    system_notice messages should only be stored when the agent has an
    active conversation that the notification is relevant to.
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

        # Route based on whether the workspace has an ongoing conversation.
        # Active conversation → system_notice (LLM-visible).
        # No conversation (cleared/never started) → host_message (human-only).
        if deps.has_active_session(group_folder):
            notify = deps.broadcast_system_notice
        else:
            notify = deps.broadcast_host_message

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
