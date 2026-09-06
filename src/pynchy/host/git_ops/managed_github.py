"""GitHub validation for managed feature pull requests."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - fixed no-shell gh commands.
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pynchy.host.git_ops.repo import (
    RepoContext,
)
from pynchy.host.git_ops.worktree_sync import (
    _WorktreeContext,
)

_GitHubRunner = Callable[..., subprocess.CompletedProcess[str]]


def inspect_managed_pr(  # noqa: PLR0911 - fail-closed checks keep exact diagnostics.
    ctx: _WorktreeContext,
    repo_ctx: RepoContext,
    gh_cwd: Path,
    *,
    runner: _GitHubRunner,
    require_head: bool = True,
) -> tuple[str | None, str | None]:
    """Return a matching managed PR URL or its fail-closed diagnostic."""
    try:
        pr_check = runner(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo_ctx.slug,
                "--head",
                ctx.branch_name,
                "--state",
                "all",
                "--limit",
                "2",
                "--json",
                "url,baseRefName,baseRefOid,headRefName,headRefOid,isCrossRepository,state",
            ],
            cwd=str(gh_cwd),
            capture_output=True,
            text=True,
            timeout=30,
            env=ctx.env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "Publication blocked: could not inspect existing managed feature PR."
    if pr_check.returncode != 0:
        return None, "Publication blocked: could not inspect existing managed feature PR."
    if not pr_check.stdout.strip():
        return None, "Publication blocked: existing managed feature PR lookup returned no data."
    try:
        existing_prs = json.loads(pr_check.stdout)
    except json.JSONDecodeError:
        return (
            None,
            "Publication blocked: existing managed feature PR lookup returned invalid data.",
        )
    if not isinstance(existing_prs, list):
        return (
            None,
            "Publication blocked: existing managed feature PR lookup returned invalid data.",
        )
    if not existing_prs:
        return None, None
    if len(existing_prs) != 1:
        return None, "Publication blocked: multiple managed feature PRs use this branch."
    existing_pr = existing_prs[0]
    if (
        not isinstance(existing_pr, dict)
        or existing_pr.get("baseRefName") != ctx.main_branch
        or existing_pr.get("baseRefOid") != ctx.base_sha
        or existing_pr.get("headRefName") != ctx.branch_name
        or (require_head and existing_pr.get("headRefOid") != ctx.head_sha)
        or existing_pr.get("isCrossRepository") is not False
        or existing_pr.get("state") != "OPEN"
        or not is_configured_pr_url(existing_pr.get("url"), repo_ctx)
    ):
        return (
            None,
            (
                "Publication blocked: existing PR is not open or does not match "
                "managed feature inspection."
            ),
        )
    return existing_pr["url"], None


def is_configured_pr_url(value: object, repo_ctx: RepoContext) -> bool:
    """Return whether a GitHub PR URL belongs to the configured repository."""
    return isinstance(value, str) and value.startswith(f"https://github.com/{repo_ctx.slug}/pull/")


def created_managed_pr_failure(  # noqa: PLR0913 - compensation needs the validated PR context.
    pr_url: str,
    reason: str,
    repo_ctx: RepoContext,
    gh_cwd: Path,
    env: dict[str, str] | None,
    *,
    runner: _GitHubRunner,
) -> dict[str, Any]:
    """Fail closed and compensate if a newly-created managed PR cannot verify."""
    closed = _close_created_managed_pr(
        pr_url,
        repo_ctx,
        gh_cwd,
        env,
        runner=runner,
    )
    suffix = " The host closed the newly created PR." if closed else " The host could not close it."
    return {"success": False, "message": f"{reason}{suffix}"}


def _close_created_managed_pr(
    pr_url: str,
    repo_ctx: RepoContext,
    gh_cwd: Path,
    env: dict[str, str] | None,
    *,
    runner: _GitHubRunner,
) -> bool:
    """Close only a newly-created PR bound to the configured repository."""
    if not is_configured_pr_url(pr_url, repo_ctx):
        return False
    try:
        closed = runner(
            ["gh", "pr", "close", pr_url, "--repo", repo_ctx.slug],
            cwd=str(gh_cwd),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return closed.returncode == 0
