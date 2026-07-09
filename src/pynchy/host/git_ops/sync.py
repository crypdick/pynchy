"""Coordinated git sync between host and container worktrees.

Host owns main — agents never push to main directly. The host mediates
all merges into main, pushes to origin, and syncs other running agents.

Container-side errors must be self-contained and actionable since
containers can't read host state (logs, config, etc.).
"""

from __future__ import annotations

import dataclasses
import subprocess  # noqa: S404, RUF100 - PR helpers use fixed no-shell gh argv.
from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves git sync signatures at runtime.
)
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves git sync signatures at runtime.
from typing import Any, Protocol, runtime_checkable

from pynchy.host.git_ops.repo import (
    RepoContext,  # noqa: TC001, RUF100 - beartype resolves git sync signatures at runtime.
)
from pynchy.host.git_ops.utils import (
    count_commits,
    detect_main_branch,
    git_env_with_token,
    push_local_commits,
    run_git,
)
from pynchy.logger import logger
from pynchy.types import (
    WorkspaceProfile,  # noqa: TC001, RUF100 - beartype resolves git sync signatures at runtime.
)

# Valid git_policy values
GIT_POLICY_MERGE = "merge-to-main"
GIT_POLICY_PR = "pull-request"


@runtime_checkable
class GitSyncDeps(Protocol):
    """Dependencies for the git sync loop."""

    async def broadcast_host_message(self, jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, jid: str, text: str) -> None: ...

    def has_active_session(self, group_folder: str) -> bool: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None: ...


# ---------------------------------------------------------------------------
# Shared precondition validation for worktree sync operations
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _WorktreeContext:
    """Validated context for worktree sync operations."""

    worktree_path: Path
    branch_name: str
    main_branch: str
    env: dict[str, str] | None
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
    env = git_env_with_token(repo_ctx.slug, group_folder=group_folder)

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

    ahead = count_commits(f"{main_branch}..{branch_name}", cwd=repo_ctx.root)
    if ahead is None:
        return {
            "success": False,
            "message": (
                "Failed to check commits on your branch. "
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

    steps: tuple[Callable[[], dict[str, Any] | None], ...] = (
        lambda: _sync_fetch_origin(repo_ctx, ctx),
        lambda: _sync_rebase_main(repo_ctx, ctx),
        lambda: _sync_rebase_worktree(ctx),
        lambda: _sync_merge_worktree(repo_ctx, ctx),
        lambda: _sync_push_main(repo_ctx, ctx),
    )
    for step in steps:
        failure = step()
        if failure is not None:
            return failure

    logger.info(
        "Worktree synced to main and pushed",
        group=group_folder,
        commits=ctx.ahead,
    )
    return {
        "success": True,
        "message": f"Merged {ctx.ahead} commit(s) into main and pushed to origin.",
    }


def _sync_fetch_origin(repo_ctx: RepoContext, ctx: _WorktreeContext) -> dict[str, Any] | None:
    fetch = run_git("fetch", "origin", cwd=repo_ctx.root, env=ctx.env)
    if fetch.returncode != 0:
        return {
            "success": False,
            "message": (
                f"git fetch failed: {fetch.stderr.strip()}. "
                "Check network connectivity and try again."
            ),
        }
    return None


def _sync_rebase_main(repo_ctx: RepoContext, ctx: _WorktreeContext) -> dict[str, Any] | None:
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
    return None


def _sync_rebase_worktree(ctx: _WorktreeContext) -> dict[str, Any] | None:
    rebase_wt = run_git("rebase", ctx.main_branch, cwd=ctx.worktree_path)
    if rebase_wt.returncode != 0:
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
    return None


def _sync_merge_worktree(repo_ctx: RepoContext, ctx: _WorktreeContext) -> dict[str, Any] | None:
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
    return None


def _sync_push_main(repo_ctx: RepoContext, ctx: _WorktreeContext) -> dict[str, Any] | None:
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
    return None


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------


def resolve_git_policy(_group_folder: str) -> str:
    """Resolve the effective git policy for a workspace.

    The config schema exposes one first-class git policy: merge to main.
    """
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
    pr_check = subprocess.run(  # noqa: S603, RUF100 - branch name comes from validated worktree context and no shell is used.
        ["gh", "pr", "view", ctx.branch_name, "--json", "url", "--jq", ".url"],  # noqa: S607, RUF100 - gh is the trusted host GitHub CLI.
        cwd=str(repo_ctx.root),
        capture_output=True,
        text=True,
        timeout=30,
        env=ctx.env,
        check=False,
    )

    if pr_check.returncode == 0 and pr_check.stdout.strip():
        pr_url = pr_check.stdout.strip()
        return {
            "success": True,
            "message": f"Pushed {ctx.ahead} commit(s) to {ctx.branch_name}. PR updated: {pr_url}",
        }

    # 3. Create a PR
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

    pr_create = subprocess.run(  # noqa: S603, RUF100 - PR fields are argv elements, not shell-interpreted.
        [  # noqa: S607, RUF100 - gh is the trusted host GitHub CLI.
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
        check=False,
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
