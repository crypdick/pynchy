"""Manifest and path validation for managed feature publication."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from pynchy.host.git_ops.repo import (
    RepoContext,
)
from pynchy.host.git_ops.utils import run_git

_MANIFEST_VERSION = 2


class _ManifestValidationError(ValueError):
    """Reject an invalid manifest entry without exposing host filesystem details."""


def _configured_repo_root(repo_ctx: RepoContext) -> Path:
    try:
        return repo_ctx.root.resolve(strict=True)
    except OSError as exc:
        raise _ManifestValidationError(
            f"Publication blocked: configured repository {repo_ctx.slug!r} is unavailable."
        ) from exc


def _load_feature_record(
    feature_slug: str,
    repo_ctx: RepoContext,
    repo_root: Path,
) -> Mapping[str, object] | None:
    manifest_path = repo_root / ".new-feature" / "manifest.toml"
    if not manifest_path.is_file():
        return None
    try:
        with manifest_path.open("rb") as manifest_file:
            manifest = tomllib.load(manifest_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _ManifestValidationError(
            f"Publication blocked: managed-feature manifest for {repo_ctx.slug!r} is unreadable."
        ) from exc

    features = manifest.get("features")
    if not isinstance(features, Mapping):
        return None
    key = feature_slug.replace("-", "_")
    record = features.get(key)
    matching_keys = [
        record_key
        for record_key, candidate in features.items()
        if isinstance(record_key, str)
        and isinstance(candidate, Mapping)
        and candidate.get("slug") == feature_slug
    ]
    if record is None and not matching_keys:
        return None
    if (
        matching_keys != [key]
        or not isinstance(record, Mapping)
        or record.get("slug") != feature_slug
    ):
        raise _ManifestValidationError(
            "Publication blocked: managed feature "
            f"{feature_slug!r} has an invalid manifest identity."
        )
    if manifest.get("version") != _MANIFEST_VERSION:
        raise _ManifestValidationError(
            "Publication blocked: managed feature "
            f"{feature_slug!r} requires manifest version {_MANIFEST_VERSION}."
        )
    if record.get("status") != "active":
        raise _ManifestValidationError(
            f"Publication blocked: managed feature {feature_slug!r} is not active."
        )
    return record


def _required_branch(
    record: Mapping[str, object],
    field: str,
    repo_ctx: RepoContext,
    repo_root: Path,
) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise _ManifestValidationError(
            "Publication blocked: managed feature manifest for "
            f"{repo_ctx.slug!r} has no valid {field}."
        )
    result = run_git("check-ref-format", "--branch", value, cwd=repo_root)
    if result.returncode != 0:
        raise _ManifestValidationError(
            "Publication blocked: managed feature manifest for "
            f"{repo_ctx.slug!r} has no valid {field}."
        )
    return value


def _managed_worktree_path(
    feature_slug: str,
    record: Mapping[str, object],
    repo_root: Path,
) -> Path:
    recorded_worktree = record.get("worktree")
    expected_relative = Path(".worktrees") / feature_slug
    if (
        not isinstance(recorded_worktree, str)
        or Path(recorded_worktree).is_absolute()
        or Path(recorded_worktree).parts != expected_relative.parts
    ):
        raise _ManifestValidationError(
            f"Publication blocked: managed feature {feature_slug!r} has an invalid worktree path."
        )

    worktrees_root = repo_root / ".worktrees"
    expected_path = worktrees_root / feature_slug
    if worktrees_root.is_symlink() or expected_path.is_symlink():
        raise _ManifestValidationError(
            f"Publication blocked: managed feature {feature_slug!r} worktree must not be a symlink."
        )
    try:
        worktree_path = expected_path.resolve(strict=True)
    except OSError as exc:
        raise _ManifestValidationError(
            f"Publication blocked: managed feature {feature_slug!r} worktree is missing or stale."
        ) from exc
    if worktree_path != expected_path or not worktree_path.is_dir():
        raise _ManifestValidationError(
            f"Publication blocked: managed feature {feature_slug!r} worktree is invalid."
        )
    return worktree_path


def _validate_git_identity(
    worktree_path: Path,
    branch_name: str,
    repo_ctx: RepoContext,
    repo_root: Path,
    feature_slug: str,
) -> Path:
    top_level = _git_path(worktree_path, "--show-toplevel")
    if top_level != worktree_path:
        raise _ManifestValidationError(
            "Publication blocked: managed feature "
            f"{feature_slug!r} worktree does not match its manifest."
        )
    worktree_common_dir = _git_path(worktree_path, "--git-common-dir")
    repo_common_dir = _git_path(repo_root, "--git-common-dir")
    if (
        worktree_common_dir is None
        or repo_common_dir is None
        or worktree_common_dir != repo_common_dir
    ):
        raise _ManifestValidationError(
            "Publication blocked: managed feature "
            f"{feature_slug!r} does not belong to {repo_ctx.slug!r}."
        )

    branch_result = run_git("branch", "--show-current", cwd=worktree_path)
    if branch_result.returncode != 0 or branch_result.stdout.strip() != branch_name:
        raise _ManifestValidationError(
            "Publication blocked: managed feature "
            f"{feature_slug!r} is not checked out on its manifest branch."
        )
    return repo_common_dir


def _git_path(cwd: Path, argument: str) -> Path | None:
    result = run_git("rev-parse", "--path-format=absolute", argument, cwd=cwd)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return Path(result.stdout.strip()).resolve(strict=True)
    except OSError:
        return None
