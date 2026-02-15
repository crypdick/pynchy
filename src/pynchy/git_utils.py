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
