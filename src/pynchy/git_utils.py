"""Shared git helpers used by worktree, workspace_ops, and git_sync modules."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pynchy.config import PROJECT_ROOT

_SUBPROCESS_TIMEOUT = 30


def run_git(
    *args: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with standard timeout and error capture."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )


def detect_main_branch() -> str:
    """Detect the main branch name via origin/HEAD, fallback to 'main'."""
    result = run_git("symbolic-ref", "refs/remotes/origin/HEAD")
    if result.returncode == 0:
        # Output like "refs/remotes/origin/main"
        ref = result.stdout.strip()
        return ref.split("/")[-1]
    return "main"


def get_head_sha() -> str:
    """Return the current git HEAD SHA, or 'unknown' on failure."""
    try:
        result = run_git("rev-parse", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def is_repo_dirty(cwd: Path | None = None) -> bool:
    """Check if the working tree has uncommitted changes."""
    try:
        result = run_git("status", "--porcelain", cwd=cwd)
        return bool(result.stdout.strip()) if result.returncode == 0 else False
    except Exception:
        return False


def count_unpushed_commits() -> int:
    """Count commits ahead of origin/main. Returns 0 on failure."""
    try:
        main = detect_main_branch()
        result = run_git("rev-list", f"origin/{main}..HEAD", "--count")
        if result.returncode == 0:
            return int(result.stdout.strip() or "0")
    except Exception:
        pass
    return 0


def files_changed_between(old_sha: str, new_sha: str, path: str) -> bool:
    """Check if files under *path* changed between two commits."""
    result = run_git("diff", "--name-only", old_sha, new_sha, "--", path)
    return bool(result.stdout.strip()) if result.returncode == 0 else False
