"""Rebase worktrees onto main and notify agents of active-session changes.

Extracted from sync.py — this is the "pull main INTO worktrees" direction,
while sync.py handles "push worktree changes INTO main."

Used by the Temporal git-sync activity and
ipc/_handlers_lifecycle.py (after a sync_worktree_to_main merge).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from pynchy.host.git_ops.repo import (
    RepoContext,  # noqa: TC001 - beartype resolves git sync helper signatures at runtime.
)
from pynchy.host.git_ops.utils import count_commits, detect_main_branch, get_head_sha, run_git
from pynchy.logger import logger
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)


@runtime_checkable
class WorktreeNotifyDeps(Protocol):
    """Narrow dependency protocol for worktree rebase notifications.

    Subset of GitSyncDeps — only the methods host_notify_worktree_updates()
    actually uses.  This allows IpcDeps (and any other superset) to satisfy
    the protocol directly without adapter boilerplate.
    """

    async def broadcast_host_message(self, jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, jid: str, text: str) -> None: ...

    async def wake_worktree_conflict(self, jid: str) -> None: ...

    def has_active_session(self, group_folder: str) -> bool: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...


# Track the last HEAD SHA for which worktree notifications were sent, per repo root.
# This prevents the poll loop from re-notifying when the IPC handler
# (sync_worktree_to_main) already notified for the same merge.
last_notified_sha: dict[str, str] = {}

type _NotifyFn = Callable[[str, str], Awaitable[None]]
type _WakeFn = Callable[[str], Awaitable[None]]


def build_rebase_notice(worktree_path: Path, old_head: str, commit_count: int) -> str:
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
        parts.append(stat_lines[-1].strip())

    if commit_count == 1:
        # Show full commit message for single commits
        msg = run_git("log", "-1", "--format=%B", cwd=worktree_path)
        if msg.returncode == 0 and msg.stdout.strip():
            parts.append(f"Commit: {msg.stdout.strip()}")
    else:
        parts.append("Run `git log --oneline -5` to see what changed.")

    return "\n".join(parts)


def _folder_to_jid_map(registered: dict[str, WorkspaceProfile]) -> dict[str, str]:
    return {group.folder: jid for jid, group in registered.items()}


def _notify_fn(
    deps: WorktreeNotifyDeps,
    group_folder: str,
) -> _NotifyFn:
    if deps.has_active_session(group_folder):
        return deps.broadcast_system_notice
    return deps.broadcast_host_message


def _worktree_jid(
    folder_to_jid: dict[str, str],
    group_folder: str,
    exclude_group: str | None,
) -> str | None:
    if group_folder == exclude_group:
        return None
    return folder_to_jid.get(group_folder)


def _dirty_worktree_notice() -> str:
    return (
        "Main branch has been updated, but your worktree has "
        "uncommitted changes. Commit or stash your work, then call "
        "sync_worktree_to_main to get the latest changes."
    )


def _conflicted_rebase_notice() -> str:
    return (
        "Main branch was updated but your worktree has "
        "rebase conflicts. Run `git status` to see conflicted files, "
        "resolve them, then `git add` and `git rebase --continue`."
    )


def _behind_commit_count(
    repo_ctx: RepoContext,
    main_branch: str,
    group_folder: str,
) -> int:
    branch_name = f"worktree/{group_folder}"
    behind_n = count_commits(f"{branch_name}..{main_branch}", cwd=repo_ctx.root)
    return behind_n or 0


def _has_unresolved_rebase(worktree_path: Path) -> bool:
    """Return whether Git still records a rebase for this linked worktree."""
    for state_dir in ("rebase-merge", "rebase-apply"):
        result = run_git("rev-parse", "--git-path", state_dir, cwd=worktree_path)
        state_path = result.stdout.strip()
        if result.returncode == 0 and state_path and Path(state_path).exists():
            return True
    return False


async def _notify_dirty_worktree(
    *,
    notify: _NotifyFn,
    jid: str,
    group_folder: str,
) -> None:
    await notify(jid, _dirty_worktree_notice())
    logger.info(
        "Skipped dirty worktree rebase, notified agent",
        group=group_folder,
    )


async def _rebase_and_notify(  # noqa: PLR0913 - local notification helper keeps the rebase inputs explicit.
    *,
    conflict_notify: _NotifyFn,
    success_notify: _NotifyFn | None,
    wake_conflict: _WakeFn | None,
    jid: str,
    group_folder: str,
    entry: Path,
    main_branch: str,
    behind_count: int,
) -> None:
    head_before = run_git("rev-parse", "HEAD", cwd=entry).stdout.strip()
    rebase = run_git("rebase", main_branch, cwd=entry)
    if rebase.returncode != 0:
        await conflict_notify(jid, _conflicted_rebase_notice())
        if wake_conflict:
            await wake_conflict(jid)
        logger.warning(
            "Worktree rebase conflict during broadcast",
            group=group_folder,
            error=rebase.stderr.strip(),
        )
        return

    if success_notify:
        await success_notify(jid, build_rebase_notice(entry, head_before, behind_count))
    logger.info("Auto-rebased worktree", group=group_folder)


async def host_notify_worktree_updates(
    exclude_group: str | None,
    deps: WorktreeNotifyDeps,
    repo_ctx: RepoContext,
) -> None:
    """Host-side: rebase all worktrees for a repo onto main, notify agents.

    For each worktree (excluding source):
    - Up to date: no notification
    - Clean + rebase succeeds: system_notice only when an active session exists
    - Clean + rebase fails: DON'T abort — notify "conflicts, run git status to fix"
    - Dirty (uncommitted): skip rebase, notify "commit or stash, then sync"

    Notification routing depends on session state:
    - Active conversation (has message history, regardless of whether the
      container is currently running): system_notice → LLM sees it on next
      wake, so it can act on conflicts or review cleanly rebased changes.
    - No conversation (session was cleared or never started — no message
      history): host_message → human sees actionable notices in the channel,
      but the LLM never does. Clean rebase FYIs are suppressed instead of
      being sent as host messages.

    This distinction matters because system_notices persist in the DB and
    become part of the conversation history. If a workspace has no ongoing
    conversation, stale notices pollute the start of the next session. The
    agent gets current worktree state from ephemeral system_notices in
    agent_runner.py at container launch — those are always fresh.
    """
    if not repo_ctx.worktrees_dir.exists():
        return

    main_branch = detect_main_branch(cwd=repo_ctx.root)
    registered = deps.workspaces()
    folder_to_jid = _folder_to_jid_map(registered)

    for entry in sorted(repo_ctx.worktrees_dir.iterdir()):
        if not entry.is_dir():
            continue

        group_folder = entry.name
        jid = _worktree_jid(folder_to_jid, group_folder, exclude_group)
        if not jid:
            continue

        behind_count = _behind_commit_count(repo_ctx, main_branch, group_folder)
        if behind_count == 0:
            continue  # up to date or can't check

        if _has_unresolved_rebase(entry):
            logger.info("Skipped unresolved worktree rebase", group=group_folder)
            continue

        notify = _notify_fn(deps, group_folder)

        status = run_git("status", "--porcelain", cwd=entry)
        if status.returncode == 0 and status.stdout.strip():
            await _notify_dirty_worktree(
                notify=notify,
                jid=jid,
                group_folder=group_folder,
            )
            continue

        clean_rebase_notify = notify if deps.has_active_session(group_folder) else None
        await _rebase_and_notify(
            conflict_notify=notify,
            success_notify=clean_rebase_notify,
            wake_conflict=(
                deps.wake_worktree_conflict if deps.has_active_session(group_folder) else None
            ),
            jid=jid,
            group_folder=group_folder,
            entry=entry,
            main_branch=main_branch,
            behind_count=behind_count,
        )

    # Record current HEAD so the poll loop can skip duplicate notifications
    # for the same merge (e.g. IPC handler already notified, poll loop detects
    # the same HEAD change seconds later).
    current_head = get_head_sha(cwd=repo_ctx.root)
    if current_head != "unknown":
        last_notified_sha[str(repo_ctx.root)] = current_head
