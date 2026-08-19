"""Resolve managed feature worktrees without trusting agent-provided locations."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess  # noqa: S404 - streams fixed Git commands between host-owned repositories.
import tempfile
from collections.abc import (
    Iterator,  # noqa: TC003 - beartype resolves managed-feature iterator annotations at runtime.
    Sequence,  # noqa: TC003 - beartype resolves managed-feature resolver annotations at runtime.
)
from dataclasses import dataclass
from pathlib import Path

from pynchy.host.git_ops._bounded_git import run_git_bounded_stdout
from pynchy.host.git_ops.managed_feature_manifest import (
    _configured_repo_root,
    _load_feature_record,
    _managed_worktree_path,
    _ManifestValidationError,
    _required_branch,
    _validate_git_identity,
)
from pynchy.host.git_ops.managed_feature_models import (
    ManagedFeaturePublication,
    ManagedFeatureResolution,
)
from pynchy.host.git_ops.managed_transport import managed_object_store_is_safe
from pynchy.host.git_ops.repo import (
    RepoContext,  # noqa: TC001 - beartype resolves managed-feature signatures at runtime.
)
from pynchy.host.git_ops.utils import (
    git_env_with_token,
    git_env_without_credentials,
    redact_git_diagnostic,
    run_git,
)

_FEATURE_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_HEAD_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_OBJECT_FORMATS = frozenset({"sha1", "sha256"})
_NO_REPLACE_OBJECTS_ENV = {"GIT_NO_REPLACE_OBJECTS": "1"}
_MAX_COP_PATCH_BYTES = 64 * 1024
_COP_PATCH_LIMIT_DIAGNOSTIC = "Committed patch exceeds the Cop inspection context limit"


@dataclass(frozen=True)
class _ManagedGitTransport:
    """Fresh Git metadata for inspecting or transporting managed feature objects."""

    root: Path
    bare_dir: Path
    repo_slug: str
    object_dir: Path
    base_env: dict[str, str]

    @property
    def args(self) -> tuple[str, ...]:
        """Return Git global options that cannot read agent-owned metadata."""
        return (
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            f"--git-dir={self.bare_dir}",
        )

    def environment(self) -> dict[str, str]:
        """Build one safe alternate-object environment immediately before Git runs."""
        if not managed_object_store_is_safe(self.object_dir):
            raise _ManifestValidationError(
                "Publication blocked: configured repository "
                f"{self.repo_slug!r} object store is unavailable."
            )
        env = dict(self.base_env)
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(self.object_dir)
        return env


def managed_feature_remote_url(repo_ctx: RepoContext) -> str:
    """Return the host-configured GitHub endpoint for managed publication."""
    return f"https://github.com/{repo_ctx.slug}.git"


def is_managed_feature_slug(value: object) -> bool:
    """Return whether ``value`` is a canonical managed-feature slug."""
    return isinstance(value, str) and _FEATURE_SLUG.fullmatch(value) is not None


def resolve_managed_feature_publication(
    feature_slug: str,
    repo_contexts: Sequence[RepoContext],
) -> ManagedFeatureResolution:
    """Resolve exactly one active feature from configured repository manifests.

    The feature's worktree is always derived from its configured repository root;
    this function never enumerates or falls back across worktree directories.
    """
    return _resolve_managed_feature(feature_slug, repo_contexts, require_rebased=True)


def _resolve_managed_feature(
    feature_slug: str,
    repo_contexts: Sequence[RepoContext],
    *,
    require_rebased: bool,
) -> ManagedFeatureResolution:
    """Resolve one manifest-bound feature, optionally before it reaches the remote base."""
    if not is_managed_feature_slug(feature_slug):
        return ManagedFeatureResolution(
            publication=None,
            error="Publication blocked: feature_slug must be a canonical managed-feature slug.",
        )

    publications: list[ManagedFeaturePublication] = []
    for repo_ctx in repo_contexts:
        try:
            publication = _resolve_repo_feature(
                feature_slug,
                repo_ctx,
                require_rebased=require_rebased,
            )
        except _ManifestValidationError as exc:
            return ManagedFeatureResolution(publication=None, error=str(exc))
        if publication is not None:
            publications.append(publication)

    if not publications:
        return ManagedFeatureResolution(
            publication=None,
            error=(
                "Publication blocked: managed feature "
                f"{feature_slug!r} is not active in a configured repository."
            ),
        )
    if len(publications) != 1:
        return ManagedFeatureResolution(
            publication=None,
            error=(
                "Publication blocked: managed feature "
                f"{feature_slug!r} is ambiguous across configured repositories."
            ),
        )
    return ManagedFeatureResolution(publication=publications[0], error=None)


def _resolve_repo_feature(
    feature_slug: str,
    repo_ctx: RepoContext,
    *,
    require_rebased: bool,
) -> ManagedFeaturePublication | None:
    repo_root = _configured_repo_root(repo_ctx)
    record = _load_feature_record(feature_slug, repo_ctx, repo_root)
    if record is None:
        return None

    branch_name = _required_branch(record, "branch", repo_ctx, repo_root)
    target_branch = _required_branch(record, "target_branch", repo_ctx, repo_root)

    worktree_path = _managed_worktree_path(feature_slug, record, repo_root)
    git_common_dir = _validate_git_identity(
        worktree_path,
        branch_name,
        repo_ctx,
        repo_root,
        feature_slug,
    )

    head_result = run_git(
        "--no-replace-objects",
        "rev-parse",
        "HEAD",
        cwd=worktree_path,
        env=_NO_REPLACE_OBJECTS_ENV,
    )
    head_sha = head_result.stdout.strip()
    if head_result.returncode != 0 or _HEAD_SHA.fullmatch(head_sha) is None:
        raise _ManifestValidationError(
            f"Publication blocked: could not verify HEAD for managed feature {feature_slug!r}."
        )

    object_format_result = run_git(
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
        "rev-parse",
        "--show-object-format=storage",
        cwd=repo_root,
        env=_NO_REPLACE_OBJECTS_ENV,
    )
    object_format = object_format_result.stdout.strip()
    if object_format_result.returncode != 0 or object_format not in _OBJECT_FORMATS:
        raise _ManifestValidationError(
            f"Publication blocked: could not verify Git object format for {feature_slug!r}."
        )

    remote_url = managed_feature_remote_url(repo_ctx)
    with _isolated_managed_git(repo_ctx, git_common_dir, object_format) as transport:
        main_branch = _remote_default_branch(transport, remote_url)
        if main_branch is None or target_branch != main_branch:
            raise _ManifestValidationError(
                f"Publication blocked: managed feature {feature_slug!r} targets {target_branch!r}, "
                "not the configured remote default branch."
            )
        base_sha = _fetch_remote_ref(
            transport,
            remote_url,
            f"refs/heads/{main_branch}",
            "refs/pynchy/managed-base",
        )
        if base_sha is None:
            raise _ManifestValidationError(
                f"Publication blocked: could not verify base for managed feature {feature_slug!r}."
            )
        if require_rebased and not _head_descends_from(base_sha, head_sha, transport):
            raise _ManifestValidationError(
                "Publication blocked: managed feature must be rebased on the remote "
                f"default branch {main_branch!r}."
            )
        ahead = _count_raw_commits(base_sha, head_sha, transport)

    if ahead is None:
        raise _ManifestValidationError(
            f"Publication blocked: could not verify commits for managed feature {feature_slug!r}."
        )
    if ahead == 0:
        raise _ManifestValidationError(
            "Publication blocked: managed feature "
            f"{feature_slug!r} has no commits ahead of {main_branch}."
        )

    publication = ManagedFeaturePublication(
        repo_ctx=repo_ctx,
        feature_slug=feature_slug,
        worktree_path=worktree_path,
        branch_name=branch_name,
        main_branch=main_branch,
        remote_url=remote_url,
        base_sha=base_sha,
        head_sha=head_sha,
        object_format=object_format,
        ahead=ahead,
        git_common_dir=git_common_dir,
    )
    _validate_clean_worktree(publication)
    return publication


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
        _validate_clean_worktree(publication)
        return _rebase_validated_managed_feature(publication)
    except _ManifestValidationError as exc:
        return {"success": False, "message": str(exc)}


def _rebase_validated_managed_feature(
    publication: ManagedFeaturePublication,
) -> dict[str, object]:
    """Fetch the exact base into host-owned metadata, then rebase once."""
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
        result = _run_managed_feature_rebase(publication, _managed_rebase_environment())
    if result.returncode == 0:
        return {
            "success": True,
            "message": (
                f"Rebased managed feature {publication.feature_slug!r} onto remote default branch "
                f"{publication.main_branch!r}."
            ),
        }
    if _has_rebase_in_progress(publication.worktree_path):
        return {
            "success": False,
            "message": (
                "Managed feature rebase has conflicts. Resolve them, then run "
                "git rebase --continue or git rebase --abort."
            ),
        }
    diagnostic = redact_git_diagnostic(result.stderr)
    return {
        "success": False,
        "message": f"Rebase blocked: {diagnostic or 'git rebase failed.'}",
    }


def _run_managed_feature_rebase(
    publication: ManagedFeaturePublication,
    environment: dict[str, str],
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
        env=environment,
    )


def _persist_verified_base(
    publication: ManagedFeaturePublication,
    transport: _ManagedGitTransport,
) -> bool:
    """Copy the isolated remote base into the validated object store for conflict recovery."""
    try:
        source = subprocess.Popen(  # noqa: S603 - fixed Git argv over host-owned metadata.
            [  # noqa: S607 - Git executable is an application prerequisite.
                "git",
                *transport.args,
                "pack-objects",
                "--stdout",
                "--revs",
            ],
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


def _managed_feature_head_is_current(publication: ManagedFeaturePublication) -> bool:
    """Reject a branch or checkout change after the host finished validation."""
    head = run_git(
        "--no-replace-objects",
        "rev-parse",
        "HEAD",
        cwd=publication.worktree_path,
        env=_NO_REPLACE_OBJECTS_ENV,
    )
    branch = run_git(
        "--no-replace-objects",
        "branch",
        "--show-current",
        cwd=publication.worktree_path,
        env=_NO_REPLACE_OBJECTS_ENV,
    )
    return (
        head.returncode == 0
        and head.stdout.strip() == publication.head_sha
        and branch.returncode == 0
        and branch.stdout.strip() == publication.branch_name
    )


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


def _has_rebase_in_progress(worktree_path: Path) -> bool:
    """Detect a recoverable rebase without reading worktree Git configuration."""
    for state_dir in ("rebase-merge", "rebase-apply"):
        state = run_git(
            "--no-replace-objects",
            "rev-parse",
            "--git-path",
            state_dir,
            cwd=worktree_path,
            env=_NO_REPLACE_OBJECTS_ENV,
        )
        if state.returncode == 0 and state.stdout.strip() and Path(state.stdout.strip()).exists():
            return True
    return False


def _count_raw_commits(
    base_sha: str,
    head_sha: str,
    transport: _ManagedGitTransport,
) -> int | None:
    """Count commits without allowing agent-controlled replacement refs."""
    result = run_git(
        *transport.args,
        "rev-list",
        f"{base_sha}..{head_sha}",
        "--count",
        cwd=transport.root,
        env=transport.environment(),
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return None


def _head_descends_from(
    base_sha: str,
    head_sha: str,
    transport: _ManagedGitTransport,
) -> bool:
    """Require the inspected feature to contain the exact remote target commit."""
    result = run_git(
        *transport.args,
        "merge-base",
        "--is-ancestor",
        base_sha,
        head_sha,
        cwd=transport.root,
        env=transport.environment(),
    )
    return result.returncode == 0


@contextlib.contextmanager
def _isolated_managed_git(
    repo_ctx: RepoContext,
    git_common_dir: Path,
    object_format: str,
) -> Iterator[_ManagedGitTransport]:
    """Create host-owned Git metadata with access only to validated objects."""
    object_dir = git_common_dir / "objects"
    if not managed_object_store_is_safe(object_dir):
        raise _ManifestValidationError(
            "Publication blocked: configured repository "
            f"{repo_ctx.slug!r} object store is unavailable."
        )
    base_env = dict(git_env_with_token(repo_ctx.slug) or {})
    base_env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            **_NO_REPLACE_OBJECTS_ENV,
        }
    )
    with tempfile.TemporaryDirectory(prefix="pynchy-managed-git-") as temp_dir:
        root = Path(temp_dir)
        bare_dir = root / "repository.git"
        transport = _ManagedGitTransport(
            root=root,
            bare_dir=bare_dir,
            repo_slug=repo_ctx.slug,
            object_dir=object_dir,
            base_env=base_env,
        )
        initialized = run_git(
            "init",
            "--bare",
            f"--object-format={object_format}",
            str(bare_dir),
            cwd=root,
            env=transport.environment(),
        )
        if initialized.returncode != 0:
            raise _ManifestValidationError(
                f"Publication blocked: could not initialize isolated Git for {repo_ctx.slug!r}."
            )
        yield transport


def _remote_default_branch(transport: _ManagedGitTransport, remote_url: str) -> str | None:
    """Return the remote symbolic HEAD branch without local Git configuration."""
    result = run_git(
        *transport.args,
        "ls-remote",
        "--symref",
        remote_url,
        "HEAD",
        cwd=transport.root,
        env=transport.environment(),
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        marker = "ref: refs/heads/"
        if line.startswith(marker) and line.endswith("\tHEAD"):
            branch = line[len(marker) : -len("\tHEAD")]
            return branch or None
    return None


def _fetch_remote_ref(
    transport: _ManagedGitTransport,
    remote_url: str,
    remote_ref: str,
    local_ref: str,
) -> str | None:
    """Read and fetch one exact remote ref through isolated Git metadata."""
    remote = run_git(
        *transport.args,
        "ls-remote",
        "--refs",
        remote_url,
        remote_ref,
        cwd=transport.root,
        env=transport.environment(),
    )
    remote_sha = _remote_ref_sha(remote.stdout, remote_ref) if remote.returncode == 0 else None
    if remote_sha is None:
        return None
    fetched = run_git(
        *transport.args,
        "fetch",
        "--no-tags",
        remote_url,
        f"{remote_ref}:{local_ref}",
        cwd=transport.root,
        env=transport.environment(),
    )
    if fetched.returncode != 0:
        return None
    resolved = run_git(
        *transport.args,
        "rev-parse",
        local_ref,
        cwd=transport.root,
        env=transport.environment(),
    )
    fetched_sha = resolved.stdout.strip()
    if resolved.returncode != 0 or fetched_sha != remote_sha:
        return None
    return remote_sha


def _remote_ref_sha(output: str, remote_ref: str) -> str | None:
    """Return one remote SHA, empty when a ref does not exist."""
    lines = [line for line in output.splitlines() if line]
    if len(lines) != 1:
        return None
    sha, separator, ref = lines[0].partition("\t")
    if separator != "\t" or ref != remote_ref or _HEAD_SHA.fullmatch(sha) is None:
        return None
    return sha


def read_managed_feature_patch(
    publication: ManagedFeaturePublication,
) -> tuple[str | None, str | None]:
    """Return a raw committed patch after revalidating its remote base."""
    remote_ref = f"refs/heads/{publication.main_branch}"
    try:
        with _isolated_managed_git(
            publication.repo_ctx,
            publication.git_common_dir,
            publication.object_format,
        ) as transport:
            current_base = _fetch_remote_ref(
                transport,
                publication.remote_url,
                remote_ref,
                "refs/pynchy/managed-base",
            )
            if current_base != publication.base_sha:
                return None, "managed feature target changed after inspection"
            diff = run_git_bounded_stdout(
                *transport.args,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--unified=3",
                f"{publication.base_sha}...{publication.head_sha}",
                "--",
                max_stdout_bytes=_MAX_COP_PATCH_BYTES,
                cwd=transport.root,
                env=transport.environment(),
            )
    except _ManifestValidationError as exc:
        return None, str(exc)
    if diff.exceeded_limit:
        return None, _COP_PATCH_LIMIT_DIAGNOSTIC
    if diff.returncode != 0:
        diagnostic = redact_git_diagnostic(diff.stderr)
        return None, diagnostic or "git failed"
    return diff.stdout, None


def _validate_clean_worktree(publication: ManagedFeaturePublication) -> None:
    """Check worktree cleanliness without reading agent-owned Git configuration."""
    with _isolated_managed_git(
        publication.repo_ctx,
        publication.git_common_dir,
        publication.object_format,
    ) as transport:
        isolated_git_args = (*transport.args, f"--work-tree={publication.worktree_path}")
        read_tree = run_git(
            *isolated_git_args,
            "read-tree",
            publication.head_sha,
            cwd=transport.root,
            env=transport.environment(),
        )
        if read_tree.returncode != 0:
            raise _ManifestValidationError(
                "Publication blocked: could not inspect managed feature "
                f"{publication.feature_slug!r} status."
            )
        refreshed = run_git(
            *isolated_git_args,
            "update-index",
            "--really-refresh",
            cwd=transport.root,
            env=transport.environment(),
        )
        if refreshed.returncode not in {0, 1}:
            raise _ManifestValidationError(
                "Publication blocked: could not inspect managed feature "
                f"{publication.feature_slug!r} status."
            )
        dirty = run_git(
            *isolated_git_args,
            "diff-index",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            publication.head_sha,
            "--",
            cwd=transport.root,
            env=transport.environment(),
        )
        if dirty.returncode == 1:
            raise _ManifestValidationError(
                "Publication blocked: managed feature has uncommitted changes. "
                "Commit all changes first."
            )
        if dirty.returncode != 0:
            raise _ManifestValidationError(
                "Publication blocked: could not inspect managed feature "
                f"{publication.feature_slug!r} status."
            )
        untracked = run_git(
            *isolated_git_args,
            "ls-files",
            "--others",
            "--exclude-standard",
            cwd=transport.root,
            env=transport.environment(),
        )
        if untracked.returncode != 0:
            raise _ManifestValidationError(
                "Publication blocked: could not inspect managed feature "
                f"{publication.feature_slug!r} status."
            )
        if untracked.stdout.strip():
            raise _ManifestValidationError(
                "Publication blocked: managed feature has uncommitted changes. "
                "Commit all changes first."
            )
