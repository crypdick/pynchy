"""Host-mediated pull request publication from isolated worktrees.

The host owns publication credentials and pushes branches, never main.

Container-side errors must be self-contained and actionable since
containers can't read host state (logs, config, etc.).
"""

from __future__ import annotations

import subprocess  # noqa: S404 - PR helpers use fixed no-shell gh argv.
import tempfile
from collections.abc import (
    Mapping,
    Sequence,
)
from pathlib import Path
from typing import Any

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
    RepoContext,
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
            branch_name=_existing_issue_branch(ctx, publication_branch),
            title=pr_title,
            body=pr_body,
        ),
    )


def _existing_issue_branch(ctx: _WorktreeContext, proposed: str | None) -> str | None:
    """Retain the origin branch established by an issue's first publication."""
    parts = proposed.split("/", 2) if proposed is not None else []
    if len(parts) != 3 or not parts[1].isdigit():
        return proposed
    upstream = run_git("rev-parse", "--symbolic-full-name", "@{upstream}", cwd=ctx.worktree_path)
    prefix = f"refs/remotes/origin/{parts[0]}/{parts[1]}/"
    branch = upstream.stdout.strip()
    if upstream.returncode == 0 and branch.startswith(prefix):
        return branch.removeprefix("refs/remotes/origin/")
    return proposed


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
    if (expected_binding is not None and publication.binding() != dict(expected_binding)) or (
        expected_head_sha is not None and publication.head_sha != expected_head_sha
    ):
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


def _open_or_update_pr(  # noqa: C901, PLR0911, PLR0912 - fail-closed managed PR checks need exact diagnostics.
    ctx: _WorktreeContext,
    repo_ctx: RepoContext,
    publication: PrPublication,
    gh_cwd: Path,
) -> dict[str, Any]:
    """Open or update the PR for its safely published branch."""
    branch_name = publication.branch_name or ctx.branch_name
    token = ctx.env.get("GH_TOKEN") if ctx.env is not None else None
    isolated_git_dir = gh_cwd / "repository.git"
    if ctx.head_sha is not None:
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

        existing_pr_url, existing_pr_failure = _managed_existing_pr(
            ctx,
            repo_ctx,
            gh_cwd,
            runner=subprocess.run,
        )
        if existing_pr_failure is not None:
            return {"success": False, "message": existing_pr_failure}
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
        existing_pr_url = pr_check.stdout.strip() or None if pr_check.returncode == 0 else None
    if existing_pr_url is not None:
        if ctx.head_sha is not None and not _managed_refs_match(
            ctx, isolated_git_dir, gh_cwd, git_runner=run_git
        ):
            return {
                "success": False,
                "message": "Publication blocked: managed feature refs changed after inspection.",
            }
        return {
            "success": True,
            "pr_url": existing_pr_url,
            "message": (
                f"Pushed {ctx.ahead} commit(s) to {branch_name}. PR updated: {existing_pr_url}"
            ),
        }

    text = _publication_text(ctx, publication, gh_cwd)
    if text is None:
        return {
            "success": False,
            "message": "Publication blocked: managed feature object store is unavailable.",
        }
    final_title, final_body = text

    if ctx.head_sha is not None and not _managed_refs_match(
        ctx, isolated_git_dir, gh_cwd, git_runner=run_git
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
        if ctx.head_sha is not None:
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
            verification_failure = (
                "Publication blocked: could not identify newly created managed feature PR."
            )
        else:
            verified_pr_url, verification_failure = _managed_existing_pr(
                ctx, repo_ctx, gh_cwd, runner=subprocess.run
            )
            if verification_failure is None:
                if verified_pr_url is None:
                    verification_failure = (
                        "Publication blocked: could not verify managed feature PR after creation."
                    )
                elif not _managed_refs_match(ctx, isolated_git_dir, gh_cwd, git_runner=run_git):
                    verification_failure = (
                        "Publication blocked: managed feature refs changed after inspection."
                    )
                else:
                    pr_url = verified_pr_url
        if verification_failure is not None:
            return _created_managed_pr_failure(
                pr_url,
                verification_failure,
                repo_ctx,
                gh_cwd,
                ctx.env,
                runner=subprocess.run,
            )
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


def _publication_text(
    ctx: _WorktreeContext, publication: PrPublication, gh_cwd: Path
) -> tuple[str, str] | None:
    """Build PR text, or fail closed if the managed object store becomes unavailable."""
    if ctx.head_sha is None:
        title = run_git("log", "-1", "--format=%s", cwd=ctx.worktree_path, env=None)
        generated_title = (
            title.stdout.strip() if title.returncode == 0 else publication.fallback_title
        )
        commit_summaries = run_git(
            "log",
            f"{ctx.main_branch}..{ctx.branch_name}",
            "--format=- %s",
            cwd=ctx.worktree_path,
            env=None,
        ).stdout.strip()
    else:
        metadata = []
        for log_args, limit, fallback in (
            (
                ("-1", "--format=%s", ctx.head_sha),
                _MAX_MANAGED_PR_TITLE_BYTES,
                publication.fallback_title,
            ),
            (
                (f"{ctx.base_sha}..{ctx.head_sha}", "--format=- %s"),
                _MAX_MANAGED_PR_BODY_BYTES,
                "Commit summaries omitted because they exceed host publication limits.",
            ),
        ):
            # Each read revalidates the agent-owned object store and avoids its Git config.
            env = _managed_git_env(ctx)
            if env is None:
                return None
            output = run_git_bounded_stdout(
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                f"--git-dir={gh_cwd / 'repository.git'}",
                "log",
                *log_args,
                max_stdout_bytes=limit,
                cwd=gh_cwd,
                env=env,
            )
            metadata.append(
                output.stdout.strip()
                if output.returncode == 0 and not output.exceeded_limit
                else fallback
            )
        generated_title, commit_summaries = metadata
    return (
        publication.title or generated_title,
        publication.body
        or (f"Automated PR from {publication.source_label}.\n\n### Commits\n{commit_summaries}"),
    )
