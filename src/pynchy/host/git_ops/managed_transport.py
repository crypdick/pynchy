"""Isolated Git transport for managed feature publication."""

from __future__ import annotations

import os
import re
import subprocess  # noqa: S404 - used only for Git runner return annotation.
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from pynchy.host.git_ops.utils import redact_git_diagnostic, run_git
from pynchy.host.git_ops.worktree_sync import (
    _WorktreeContext,
)

_GIT_OBJECT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def managed_object_store_is_safe(object_dir: Path | None) -> bool:
    """Return whether an alternate object store has no redirecting entries."""
    if object_dir is None:
        return False
    try:
        if object_dir.is_symlink() or not object_dir.is_dir():
            return False
        info_dir = object_dir / "info"
        alternates = info_dir / "alternates"
        if (
            info_dir.is_symlink()
            or (info_dir.exists() and not info_dir.is_dir())
            or alternates.is_symlink()
            or alternates.exists()
        ):
            return False
        return (
            object_dir.resolve(strict=True) == object_dir
            and (not info_dir.exists() or info_dir.resolve(strict=True) == info_dir)
            and _object_tree_entries_are_safe(object_dir)
        )
    except (OSError, RuntimeError):
        return False


def _object_tree_entries_are_safe(directory: Path) -> bool:
    """Reject links and special files anywhere Git can reach through the store."""
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_symlink():
                return False
            if entry.is_dir(follow_symlinks=False):
                if not _object_tree_entries_are_safe(Path(entry.path)):
                    return False
            elif not entry.is_file(follow_symlinks=False):
                return False
    return True


def _managed_git_env(ctx: _WorktreeContext) -> dict[str, str] | None:
    """Build isolated transport environment without host Git configuration."""
    object_dir = ctx.object_dir
    if object_dir is None or not managed_object_store_is_safe(object_dir):
        return None
    env = dict(ctx.env) if ctx.env is not None else {}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(object_dir)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _run_managed_git(
    ctx: _WorktreeContext,
    *args: str,
    cwd: Path,
    git_runner: _GitRunner,
) -> subprocess.CompletedProcess[str] | None:
    """Run one isolated Git command after a last-moment object-store check."""
    env = _managed_git_env(ctx)
    if env is None:
        return None
    return git_runner(*args, cwd=cwd, env=env)


def _push_managed_feature(  # noqa: PLR0911, PLR0912 - fail-closed remote checks need exact diagnostics.
    ctx: _WorktreeContext,
    isolated_dir: Path,
    token: str | None,
    *,
    git_runner: _GitRunner = run_git,
) -> dict[str, Any] | None:
    """Push an inspected SHA without loading the managed worktree's Git config."""
    object_format = cast("str", ctx.object_format)
    remote_url = cast("str", ctx.remote_url)
    base_sha = cast("str", ctx.base_sha)
    head_sha = cast("str", ctx.head_sha)
    object_store_failure = {
        "success": False,
        "message": "Publication blocked: managed feature object store is unavailable.",
    }
    bare_dir = isolated_dir / "repository.git"
    initialized = _run_managed_git(
        ctx,
        "init",
        "--bare",
        f"--object-format={object_format}",
        str(bare_dir),
        cwd=isolated_dir,
        git_runner=git_runner,
    )
    if initialized is None:
        return object_store_failure
    if initialized.returncode != 0:
        return {
            "success": False,
            "message": (
                "Publication blocked: could not initialize isolated Git transport: "
                f"{redact_git_diagnostic(initialized.stderr, token=token)}"
            ),
        }

    isolated_git_args = (
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        f"--git-dir={bare_dir}",
    )
    target_ref = f"refs/heads/{ctx.main_branch}"
    target = _run_managed_git(
        ctx,
        *isolated_git_args,
        "ls-remote",
        "--refs",
        remote_url,
        target_ref,
        cwd=isolated_dir,
        git_runner=git_runner,
    )
    if target is None:
        return object_store_failure
    target_sha = _remote_branch_sha(target.stdout, target_ref) if target.returncode == 0 else None
    if target_sha != base_sha:
        return {
            "success": False,
            "message": "Publication blocked: managed feature target changed after Cop inspection.",
        }
    fetched_base = _run_managed_git(
        ctx,
        *isolated_git_args,
        "fetch",
        "--no-tags",
        remote_url,
        f"{target_ref}:refs/pynchy/managed-base",
        cwd=isolated_dir,
        git_runner=git_runner,
    )
    if fetched_base is None:
        return object_store_failure
    if fetched_base.returncode != 0:
        return {
            "success": False,
            "message": "Publication blocked: could not fetch managed feature target.",
        }
    fetched_sha = _run_managed_git(
        ctx,
        *isolated_git_args,
        "rev-parse",
        "refs/pynchy/managed-base",
        cwd=isolated_dir,
        git_runner=git_runner,
    )
    if fetched_sha is None:
        return object_store_failure
    if fetched_sha.returncode != 0 or fetched_sha.stdout.strip() != base_sha:
        return {
            "success": False,
            "message": "Publication blocked: managed feature target changed after Cop inspection.",
        }
    remote_ref = f"refs/heads/{ctx.branch_name}"
    remote = _run_managed_git(
        ctx,
        *isolated_git_args,
        "ls-remote",
        "--refs",
        remote_url,
        remote_ref,
        cwd=isolated_dir,
        git_runner=git_runner,
    )
    if remote is None:
        return object_store_failure
    if remote.returncode != 0:
        return {
            "success": False,
            "message": (
                "Publication blocked: could not verify managed feature remote branch: "
                f"{redact_git_diagnostic(remote.stderr, token=token)}"
            ),
        }
    remote_sha = _remote_branch_sha(remote.stdout, remote_ref)
    if remote_sha is None:
        return {
            "success": False,
            "message": "Publication blocked: managed feature remote branch returned invalid data.",
        }

    push = _run_managed_git(
        ctx,
        *isolated_git_args,
        "push",
        "--no-verify",
        f"--force-with-lease={remote_ref}:{remote_sha}",
        remote_url,
        f"{head_sha}:{remote_ref}",
        cwd=isolated_dir,
        git_runner=git_runner,
    )
    if push is None:
        return object_store_failure
    if push.returncode != 0:
        return {
            "success": False,
            "message": f"Push failed: {redact_git_diagnostic(push.stderr, token=token)}",
        }
    return None


def _remote_branch_sha(output: str, remote_ref: str) -> str | None:
    """Return one remote branch SHA, empty when the branch does not yet exist."""
    lines = [line for line in output.splitlines() if line]
    if not lines:
        return ""
    if len(lines) != 1:
        return None
    sha, separator, ref = lines[0].partition("\t")
    if separator != "\t" or ref != remote_ref or _GIT_OBJECT_ID.fullmatch(sha) is None:
        return None
    return sha


def _managed_refs_match(
    ctx: _WorktreeContext,
    bare_dir: Path,
    cwd: Path,
    *,
    git_runner: _GitRunner = run_git,
) -> bool:
    """Require exact remote target and feature refs at one publication checkpoint."""
    remote_url = cast("str", ctx.remote_url)
    base_sha = cast("str", ctx.base_sha)
    head_sha = cast("str", ctx.head_sha)
    target_ref = f"refs/heads/{ctx.main_branch}"
    head_ref = f"refs/heads/{ctx.branch_name}"
    result = _run_managed_git(
        ctx,
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        f"--git-dir={bare_dir}",
        "ls-remote",
        "--refs",
        remote_url,
        target_ref,
        head_ref,
        cwd=cwd,
        git_runner=git_runner,
    )
    if result is None or result.returncode != 0:
        return False
    return _remote_refs_match(
        result.stdout,
        {target_ref: base_sha, head_ref: head_sha},
    )


def _remote_refs_match(output: str, expected: dict[str, str]) -> bool:
    """Parse one exact `ls-remote --refs` response without accepting extras."""
    actual: dict[str, str] = {}
    for line in output.splitlines():
        sha, separator, ref = line.partition("\t")
        if separator != "\t" or _GIT_OBJECT_ID.fullmatch(sha) is None or ref in actual:
            return False
        actual[ref] = sha
    return actual == expected
