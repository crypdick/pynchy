"""Rebase manifest-bound managed feature branches on verified remote bases."""

from __future__ import annotations

import subprocess  # noqa: S404 - streams fixed Git commands between host-owned repositories.
from collections.abc import Sequence
from pathlib import Path

from pynchy.host.git_ops.managed_feature import (
    _NO_REPLACE_OBJECTS_ENV,
    _fetch_remote_ref,
    _head_descends_from,
    _isolated_managed_git,
    _ManagedGitTransport,
    _resolve_managed_feature,
)
from pynchy.host.git_ops.managed_feature_manifest import _ManifestValidationError
from pynchy.host.git_ops.managed_feature_models import (
    ManagedFeaturePublication,
)
from pynchy.host.git_ops.repo import (
    RepoContext,
)
from pynchy.host.git_ops.utils import git_env_without_credentials, redact_git_diagnostic, run_git


def host_rebase_managed_feature(
    feature_slug: str,
    repo_contexts: Sequence[RepoContext],
) -> dict[str, object]:
    """Rebase one clean manifest-bound feature onto its verified remote default branch."""
    resolution = _resolve_managed_feature(feature_slug, repo_contexts, require_rebased=False)
    publication = resolution.publication
    if publication is None:
        return {"success": False, "message": resolution.error or "Rebase blocked."}
    if not _managed_feature_head_is_current(publication):
        return {
            "success": False,
            "message": "Rebase blocked: managed feature changed during validation. Retry.",
        }
    try:
        result = _prepare_rebase(publication)
    except _ManifestValidationError as exc:
        return {"success": False, "message": str(exc)}
    return _rebase_result(publication, result)


def _rebase_result(
    publication: ManagedFeaturePublication,
    result: subprocess.CompletedProcess[str] | dict[str, object],
) -> dict[str, object]:
    """Translate prepared rebase outcome into the agent-facing result."""
    if isinstance(result, dict):
        return result
    if result.returncode == 0:
        return {
            "success": True,
            "message": (
                f"Rebased managed feature {publication.feature_slug!r} onto remote default branch "
                f"{publication.main_branch!r}."
            ),
        }
    if _has_rebase_in_progress(publication):
        return {
            "success": False,
            "message": (
                "Managed feature rebase has conflicts. Resolve them, then run "
                "git rebase --continue or git rebase --abort."
            ),
        }
    diagnostic = redact_git_diagnostic(result.stderr)
    return {"success": False, "message": f"Rebase blocked: {diagnostic or 'git rebase failed.'}"}


def _prepare_rebase(
    publication: ManagedFeaturePublication,
) -> subprocess.CompletedProcess[str] | dict[str, object]:
    """Fetch current base and preserve it locally before rewriting the feature."""
    with _isolated_managed_git(
        publication.repo_ctx,
        publication.git_common_dir,
        publication.object_format,
    ) as transport:
        current_base = _fetch_remote_ref(
            transport,
            publication.remote_url,
            f"refs/heads/{publication.main_branch}",
            "refs/pynchy/managed-rebase-base",
        )
        if current_base != publication.base_sha:
            return {
                "success": False,
                "message": (
                    "Rebase blocked: remote default branch changed during validation. Retry."
                ),
            }
        if _head_descends_from(publication.base_sha, publication.head_sha, transport):
            return {
                "success": True,
                "message": (
                    f"Managed feature {publication.feature_slug!r} already includes remote default "
                    f"branch {publication.main_branch!r}."
                ),
            }
        if not _persist_verified_base(publication, transport):
            return {
                "success": False,
                "message": "Rebase blocked: could not prepare the verified remote base.",
            }
        return _run_managed_feature_rebase(publication)


def _managed_feature_head_is_current(publication: ManagedFeaturePublication) -> bool:
    """Reject a branch or checkout change after the host finished validation."""
    head = run_git("--no-replace-objects", "rev-parse", "HEAD", cwd=publication.worktree_path)
    branch = run_git(
        "--no-replace-objects", "branch", "--show-current", cwd=publication.worktree_path
    )
    return (
        head.returncode == 0
        and head.stdout.strip() == publication.head_sha
        and branch.returncode == 0
        and branch.stdout.strip() == publication.branch_name
    )


def _run_managed_feature_rebase(
    publication: ManagedFeaturePublication,
) -> subprocess.CompletedProcess[str]:
    """Run the fixed rebase command against the host-provided base object."""
    return run_git(
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.worktree={publication.worktree_path}",
        "rebase",
        "--no-verify",
        publication.base_sha,
        cwd=publication.worktree_path,
        env=_managed_rebase_environment(),
    )


def _persist_verified_base(
    publication: ManagedFeaturePublication,
    transport: _ManagedGitTransport,
) -> bool:
    """Copy isolated remote base into validated object store for conflict recovery."""
    try:
        source = subprocess.Popen(  # noqa: S603 - fixed Git argv over host-owned metadata.
            ["git", *transport.args, "pack-objects", "--stdout", "--revs"],  # noqa: S607
            cwd=transport.root,
            env=transport.environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    if source.stdin is None or source.stdout is None:
        source.kill()
        source.wait()
        return False
    try:
        source.stdin.write(f"{publication.base_sha}\n".encode())
        source.stdin.close()
        indexed = subprocess.run(  # noqa: S603 - fixed Git argv into validated Git metadata.
            [  # noqa: S607 - Git executable is an application prerequisite.
                "git",
                "--no-replace-objects",
                f"--git-dir={publication.git_common_dir}",
                "index-pack",
                "--stdin",
                "--fix-thin",
                "--keep=pynchy-managed-rebase",
            ],
            stdin=source.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            cwd=publication.worktree_path,
            env=_managed_rebase_environment(),
            timeout=30,
        )
        source.stdout.close()
        source_returncode = source.wait(timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        source.kill()
        source.wait()
        return False
    return indexed.returncode == 0 and source_returncode == 0


def _has_rebase_in_progress(publication: ManagedFeaturePublication) -> bool:
    """Detect a recoverable rebase without reading worktree Git configuration."""
    for state_dir in ("rebase-merge", "rebase-apply"):
        state = run_git(
            "--no-replace-objects",
            "rev-parse",
            "--git-path",
            state_dir,
            cwd=publication.worktree_path,
            env=_NO_REPLACE_OBJECTS_ENV,
        )
        if state.returncode == 0 and state.stdout.strip() and Path(state.stdout.strip()).exists():
            return True
    return False


def _managed_rebase_environment() -> dict[str, str]:
    """Disable ambient configuration and editors before rewriting agent-owned commits."""
    env = git_env_without_credentials()
    env.update(
        {
            "GIT_EDITOR": ":",
            "GIT_SEQUENCE_EDITOR": ":",
            "GIT_TERMINAL_PROMPT": "0",
            **_NO_REPLACE_OBJECTS_ENV,
        }
    )
    return env
