"""Host-mediated pull request publication from isolated worktrees.

The host owns publication credentials and pushes branches, never main.

Container-side errors must be self-contained and actionable since
containers can't read host state (logs, config, etc.).
"""

from __future__ import annotations

import subprocess  # noqa: S404 - PR helpers use fixed no-shell gh argv.
import tempfile
from collections.abc import (  # noqa: TC003 - beartype resolves managed publication signatures at runtime.
    Mapping,
    Sequence,
)
from pathlib import Path
from typing import Any, cast

from pynchy.host.git_ops._bounded_git import run_git_bounded_stdout
from pynchy.host.git_ops._pr_publication import PrPublication
from pynchy.host.git_ops.managed_feature import resolve_managed_feature_publication
from pynchy.host.git_ops.managed_github import (
    created_managed_pr_failure as _created_managed_pr_failure,
)
from pynchy.host.git_ops.managed_github import (
    inspect_managed_pr as _managed_existing_pr,
)
from pynchy.host.git_ops.managed_github import (
    is_configured_pr_url as _is_configured_pr_url,
)
from pynchy.host.git_ops.managed_transport import (
    _managed_git_env,
    _managed_refs_match,
    _push_managed_feature,
)
from pynchy.host.git_ops.repo import (
    RepoContext,  # noqa: TC001 - beartype resolves git sync signatures at runtime.
)
from pynchy.host.git_ops.utils import (
    git_env_with_token,
    redact_git_diagnostic,
    run_git,
)
from pynchy.host.git_ops.worktree_sync import (
    _validate_sync_preconditions,
    _WorktreeContext,
)
from pynchy.logger import logger

_MAX_MANAGED_PR_TITLE_BYTES = 256
_MAX_MANAGED_PR_BODY_BYTES = 64 * 1024

# ---------------------------------------------------------------------------
# host_create_pr_from_worktree — push branch and open/update a PR
# ---------------------------------------------------------------------------


def host_create_pr_from_worktree(
    group_folder: str,
    repo_ctx: RepoContext,
    *,
    publication_branch: str | None = None,
    pr_title: str | None = None,
    pr_body: str | None = None,
) -> dict[str, Any]:
    """Host-side: push worktree branch to origin and open/update a PR.

    Idempotent: if a PR already exists for the branch, just pushes (PR
    auto-updates). No duplicate PRs.

    Successful PR creation or update also returns its canonical ``pr_url``.
    """
    ctx = _validate_sync_preconditions(group_folder, repo_ctx)
    if isinstance(ctx, dict):
        return ctx
    return _create_pr_from_context(
        ctx,
        repo_ctx,
        PrPublication(
            source_label=f"workspace `{group_folder}`",
            fallback_title=f"Changes from {group_folder}",
            branch_name=publication_branch,
            title=pr_title,
            body=pr_body,
        ),
    )


def host_create_pr_from_managed_feature(
    feature_slug: str,
    repo_contexts: Sequence[RepoContext],
    *,
    expected_head_sha: str | None = None,
    expected_binding: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Push one freshly validated managed feature branch and open or update its PR."""
    resolution = resolve_managed_feature_publication(feature_slug, repo_contexts)
    publication = resolution.publication
    if publication is None:
        return {"success": False, "message": resolution.error or "Publication blocked."}
    if expected_binding is not None and publication.binding() != dict(expected_binding):
        return {
            "success": False,
            "message": (
                "Publication blocked: managed feature changed after Cop inspection. "
                "Inspect and publish it again."
            ),
        }
    if expected_head_sha is not None and publication.head_sha != expected_head_sha:
        return {
            "success": False,
            "message": (
                "Publication blocked: managed feature changed after Cop inspection. "
                "Inspect and publish it again."
            ),
        }
    return _create_pr_from_context(
        _WorktreeContext(
            worktree_path=publication.worktree_path,
            branch_name=publication.branch_name,
            main_branch=publication.main_branch,
            env=git_env_with_token(publication.repo_ctx.slug),
            ahead=publication.ahead,
            log_group=publication.feature_slug,
            base_sha=publication.base_sha,
            head_sha=publication.head_sha,
            object_dir=publication.git_common_dir / "objects",
            object_format=publication.object_format,
            remote_url=publication.remote_url,
        ),
        publication.repo_ctx,
        PrPublication(
            source_label=f"managed feature `{publication.feature_slug}`",
            fallback_title=f"Changes from managed feature {publication.feature_slug}",
        ),
    )


def _create_pr_from_context(
    ctx: _WorktreeContext,
    repo_ctx: RepoContext,
    publication: PrPublication,
) -> dict[str, Any]:
    """Push a validated branch and open or update its pull request."""
    token = ctx.env.get("GH_TOKEN") if ctx.env is not None else None
    branch_name = publication.branch_name or ctx.branch_name

    if ctx.head_sha is None:
        push = run_git(
            "push",
            "-u",
            "origin",
            f"{ctx.branch_name}:{branch_name}",
            "--force-with-lease",
            cwd=repo_ctx.root,
            env=ctx.env,
        )
        if push.returncode != 0:
            return {
                "success": False,
                "message": f"Push failed: {redact_git_diagnostic(push.stderr, token=token)}",
            }
        return _open_or_update_pr(
            ctx,
            repo_ctx,
            publication,
            repo_ctx.root,
        )

    # A managed worktree shares Git metadata with the agent. Publish from a
    # temporary bare repository so local remotes, hooks, and URL rewrites cannot run.
    with tempfile.TemporaryDirectory(prefix="pynchy-managed-push-") as temp_dir:
        isolated_dir = Path(temp_dir)
        _, preflight_failure = _managed_existing_pr(
            ctx,
            repo_ctx,
            isolated_dir,
            runner=subprocess.run,
            require_head=False,
        )
        if preflight_failure is not None:
            return {"success": False, "message": preflight_failure}
        push_failure = _push_managed_feature(ctx, isolated_dir, token, git_runner=run_git)
        if push_failure is not None:
            return push_failure
        return _open_or_update_pr(
            ctx,
            repo_ctx,
            publication,
            isolated_dir,
        )


def _open_or_update_pr(  # noqa: C901, PLR0911, PLR0912, PLR0915 - fail-closed managed PR checks need exact diagnostics.
    ctx: _WorktreeContext,
    repo_ctx: RepoContext,
    publication: PrPublication,
    gh_cwd: Path,
) -> dict[str, Any]:
    """Open or update the PR for its safely published branch."""
    branch_name = publication.branch_name or ctx.branch_name
    token = ctx.env.get("GH_TOKEN") if ctx.env is not None else None
    isolated_git_dir: Path | None = None
    if ctx.head_sha is not None:
        isolated_git_dir = gh_cwd / "repository.git"
        if not isolated_git_dir.is_dir():
            return {
                "success": False,
                "message": "Publication blocked: managed feature Git identity is incomplete.",
            }
        if not _managed_refs_match(ctx, isolated_git_dir, gh_cwd, git_runner=run_git):
            return {
                "success": False,
                "message": "Publication blocked: managed feature refs changed after inspection.",
            }

    if ctx.head_sha is not None:
        existing_pr_url, existing_pr_failure = _managed_existing_pr(
            ctx,
            repo_ctx,
            gh_cwd,
            runner=subprocess.run,
        )
        if existing_pr_failure is not None:
            return {"success": False, "message": existing_pr_failure}
        if existing_pr_url is not None:
            if not _managed_refs_match(
                ctx, cast("Path", isolated_git_dir), gh_cwd, git_runner=run_git
            ):
                return {
                    "success": False,
                    "message": (
                        "Publication blocked: managed feature refs changed after inspection."
                    ),
                }
            return {
                "success": True,
                "pr_url": existing_pr_url,
                "message": (
                    f"Pushed {ctx.ahead} commit(s) to {branch_name}. PR updated: {existing_pr_url}"
                ),
            }
    else:
        # env includes GH_TOKEN which gh CLI respects
        pr_check = subprocess.run(  # noqa: S603 - branch name comes from validated worktree context and no shell is used.
            [  # noqa: S607 - gh is the trusted host GitHub CLI.
                "gh",
                "pr",
                "list",
                f"--head={branch_name}",
                f"--repo={repo_ctx.slug}",
                f"--base={ctx.main_branch}",
                "--json=url",
                "--jq=.[0].url",
            ],
            cwd=str(gh_cwd),
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
                "pr_url": pr_url,
                "message": (f"Pushed {ctx.ahead} commit(s) to {branch_name}. PR updated: {pr_url}"),
            }

    title_args: tuple[str, ...] = ("log", "-1", "--format=%s")
    managed_git_args: tuple[str, ...] = ()
    managed_env: dict[str, str] | None = None
    log_cwd = ctx.worktree_path
    log_range_start = ctx.main_branch
    log_range_end = ctx.head_sha or ctx.branch_name
    if ctx.head_sha is not None:
        # Read commit text from fresh Git metadata, not agent-writable shared config.
        managed_git_args = (
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            f"--git-dir={isolated_git_dir}",
        )
        managed_env = _managed_git_env(ctx)
        if managed_env is None:
            return {
                "success": False,
                "message": "Publication blocked: managed feature object store is unavailable.",
            }
        log_cwd = gh_cwd
        log_range_start = cast("str", ctx.base_sha)
        log_range_end = ctx.head_sha
        title_args = (*title_args, ctx.head_sha)
    if ctx.head_sha is None:
        worktree_title = run_git(
            *managed_git_args,
            *title_args,
            cwd=log_cwd,
            env=managed_env,
        )
        generated_title = (
            worktree_title.stdout.strip()
            if worktree_title.returncode == 0
            else publication.fallback_title
        )
    else:
        managed_title = run_git_bounded_stdout(
            *managed_git_args,
            *title_args,
            max_stdout_bytes=_MAX_MANAGED_PR_TITLE_BYTES,
            cwd=log_cwd,
            env=managed_env,
        )
        generated_title = (
            managed_title.stdout.strip()
            if managed_title.returncode == 0 and not managed_title.exceeded_limit
            else publication.fallback_title
        )
    final_title = publication.title or generated_title

    if ctx.head_sha is not None:
        # Revalidate immediately before each isolated Git read.
        managed_env = _managed_git_env(ctx)
        if managed_env is None:
            return {
                "success": False,
                "message": "Publication blocked: managed feature object store is unavailable.",
            }
    if ctx.head_sha is None:
        worktree_body = run_git(
            *managed_git_args,
            "log",
            f"{log_range_start}..{log_range_end}",
            "--format=- %s",
            cwd=log_cwd,
            env=managed_env,
        )
        commit_summaries = worktree_body.stdout.strip()
    else:
        managed_body = run_git_bounded_stdout(
            *managed_git_args,
            "log",
            f"{log_range_start}..{log_range_end}",
            "--format=- %s",
            max_stdout_bytes=_MAX_MANAGED_PR_BODY_BYTES,
            cwd=log_cwd,
            env=managed_env,
        )
        commit_summaries = (
            managed_body.stdout.strip()
            if managed_body.returncode == 0 and not managed_body.exceeded_limit
            else "Commit summaries omitted because they exceed host publication limits."
        )
    final_body = publication.body or (
        f"Automated PR from {publication.source_label}.\n\n### Commits\n{commit_summaries}"
    )

    if ctx.head_sha is not None and not _managed_refs_match(
        ctx, cast("Path", isolated_git_dir), gh_cwd, git_runner=run_git
    ):
        return {
            "success": False,
            "message": "Publication blocked: managed feature refs changed after inspection.",
        }

    pr_create = subprocess.run(  # noqa: S603 - PR fields are argv elements, not shell-interpreted.
        [  # noqa: S607 - gh is the trusted host GitHub CLI.
            "gh",
            "pr",
            "create",
            "--repo",
            repo_ctx.slug,
            "--base",
            ctx.main_branch,
            "--head",
            branch_name,
            "--title",
            final_title,
            "--body",
            final_body,
        ],
        cwd=str(gh_cwd),
        capture_output=True,
        text=True,
        timeout=30,
        env=ctx.env,
        check=False,
    )

    if pr_create.returncode != 0:
        if ctx.head_sha is not None and isolated_git_dir is not None:
            verified_pr_url, verification_failure = _managed_existing_pr(
                ctx,
                repo_ctx,
                gh_cwd,
                runner=subprocess.run,
            )
            if (
                verification_failure is None
                and verified_pr_url is not None
                and _managed_refs_match(ctx, isolated_git_dir, gh_cwd, git_runner=run_git)
            ):
                return {
                    "success": True,
                    "pr_url": verified_pr_url,
                    "message": (
                        f"Pushed {ctx.ahead} commit(s) to {ctx.branch_name}. "
                        f"PR updated: {verified_pr_url}"
                    ),
                }
        return {
            "success": False,
            "message": (
                f"Pushed {ctx.ahead} commit(s) to {ctx.branch_name}, but PR creation failed: "
                f"{redact_git_diagnostic(pr_create.stderr, token=token)}"
            ),
        }

    pr_url = pr_create.stdout.strip()
    if ctx.head_sha is not None:
        if not _is_configured_pr_url(pr_url, repo_ctx):
            return _created_managed_pr_failure(
                pr_url,
                "Publication blocked: could not identify newly created managed feature PR.",
                repo_ctx,
                gh_cwd,
                ctx.env,
                runner=subprocess.run,
            )
        verified_pr_url, verification_failure = _managed_existing_pr(
            ctx,
            repo_ctx,
            gh_cwd,
            runner=subprocess.run,
        )
        if verification_failure is not None:
            return _created_managed_pr_failure(
                pr_url,
                verification_failure,
                repo_ctx,
                gh_cwd,
                ctx.env,
                runner=subprocess.run,
            )
        if verified_pr_url is None:
            return _created_managed_pr_failure(
                pr_url,
                "Publication blocked: could not verify managed feature PR after creation.",
                repo_ctx,
                gh_cwd,
                ctx.env,
                runner=subprocess.run,
            )
        if not _managed_refs_match(ctx, cast("Path", isolated_git_dir), gh_cwd, git_runner=run_git):
            return _created_managed_pr_failure(
                pr_url,
                "Publication blocked: managed feature refs changed after inspection.",
                repo_ctx,
                gh_cwd,
                ctx.env,
                runner=subprocess.run,
            )
        pr_url = verified_pr_url
    logger.info(
        "Worktree pushed and PR created",
        group=ctx.log_group,
        commits=ctx.ahead,
        pr_url=pr_url,
    )
    return {
        "success": True,
        "pr_url": pr_url,
        "message": f"Pushed {ctx.ahead} commit(s) to {branch_name} and opened PR: {pr_url}",
    }
