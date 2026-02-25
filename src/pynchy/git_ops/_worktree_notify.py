"""Rebase worktrees onto main and notify agents of changes.

Extracted from sync.py — this is the "pull main INTO worktrees" direction,
while sync.py handles "push worktree changes INTO main."

Used by sync_poll.py (background polling loops) and
ipc/_handlers_lifecycle.py (after a sync_worktree_to_main merge).
"""

from __future__ import annotations

from pathlib import Path

from pynchy.git_ops.repo import RepoContext
from pynchy.git_ops.sync import GitSyncDeps
from pynchy.git_ops.utils import detect_main_branch, get_head_sha, run_git
from pynchy.logger import logger

# Track the last HEAD SHA for which worktree notifications were sent, per repo root.
# This prevents the poll loop from re-notifying when the IPC handler
# (sync_worktree_to_main) already notified for the same merge.
last_notified_sha: dict[str, str] = {}


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
        last_notified_sha[str(repo_ctx.root)] = current_head
