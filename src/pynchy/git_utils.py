"""Shared git helpers used by worktree, workspace_ops, and git_sync modules."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pynchy.config import get_settings
from pynchy.logger import logger

_SUBPROCESS_TIMEOUT = 30


def run_git(
    *args: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with standard timeout and error capture."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or get_settings().project_root),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )


class GitCommandError(Exception):
    """Raised when a git command fails."""

    def __init__(self, command: str, stderr: str, returncode: int) -> None:
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"git {command} failed (exit {returncode}): {stderr}")


def require_success(result: subprocess.CompletedProcess[str], command: str) -> str:
    """Assert that a git command succeeded, raising GitCommandError otherwise.

    Returns the stripped stdout on success.
    """
    if result.returncode != 0:
        raise GitCommandError(command, result.stderr.strip(), result.returncode)
    return result.stdout.strip()


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
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("get_head_sha failed", error=str(exc))
        return "unknown"


def is_repo_dirty(cwd: Path | None = None) -> bool:
    """Check if the working tree has uncommitted changes."""
    try:
        result = run_git("status", "--porcelain", cwd=cwd)
        return bool(result.stdout.strip()) if result.returncode == 0 else False
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("is_repo_dirty failed", error=str(exc), cwd=str(cwd))
        return False


def count_unpushed_commits() -> int:
    """Count commits ahead of origin/main. Returns 0 on failure."""
    try:
        main = detect_main_branch()
        result = run_git("rev-list", f"origin/{main}..HEAD", "--count")
        if result.returncode == 0:
            return int(result.stdout.strip() or "0")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.debug("count_unpushed_commits failed", error=str(exc))
    return 0


def files_changed_between(old_sha: str, new_sha: str, path: str) -> bool:
    """Check if files under *path* changed between two commits."""
    result = run_git("diff", "--name-only", old_sha, new_sha, "--", path)
    return bool(result.stdout.strip()) if result.returncode == 0 else False


def push_local_commits(*, skip_fetch: bool = False) -> bool:
    """Best-effort push of local commits to origin/main.

    Returns True if repo is in sync (nothing to push, or push succeeded).
    Retries once on rebase failure (covers the race where origin advances
    between fetch and rebase when two worktrees push nearly simultaneously).
    Never raises — all failures are logged and return False.
    """
    try:
        if not skip_fetch:
            fetch = run_git("fetch", "origin")
            if fetch.returncode != 0:
                logger.warning("push_local: git fetch failed", stderr=fetch.stderr.strip())
                return False

        count = run_git("rev-list", "origin/main..HEAD", "--count")
        if count.returncode != 0 or int(count.stdout.strip() or "0") == 0:
            return True  # nothing to push (or can't tell)

        # Try rebase+push, retry once if origin advanced mid-operation
        for attempt in range(2):
            rebase = run_git("rebase", "origin/main")
            if rebase.returncode != 0:
                run_git("rebase", "--abort")
                if attempt == 0:
                    # Re-fetch and retry — origin may have advanced
                    logger.info("push_local: rebase failed, retrying after fresh fetch")
                    retry_fetch = run_git("fetch", "origin")
                    if retry_fetch.returncode != 0:
                        logger.warning(
                            "push_local: retry fetch failed", stderr=retry_fetch.stderr.strip()
                        )
                        return False
                    continue
                logger.warning(
                    "push_local: rebase failed after retry", stderr=rebase.stderr.strip()
                )
                return False

            push = run_git("push")
            if push.returncode != 0:
                logger.warning("push_local: git push failed", stderr=push.stderr.strip())
                return False

            logger.info("push_local: pushed local commits")
            return True

        return False  # exhausted attempts
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.warning("push_local: unexpected error", err=str(exc))
        return False
