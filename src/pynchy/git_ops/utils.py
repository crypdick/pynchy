"""Shared git helpers used by worktree, workspace_ops, and git_sync modules."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pynchy.config import get_settings
from pynchy.logger import logger

_SUBPROCESS_TIMEOUT = 30


def run_git(
    *args: str,
    cwd: Path | None = None,
    timeout: int = _SUBPROCESS_TIMEOUT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with standard timeout and error capture.

    Args:
        env: Optional environment dict for remote-facing git calls (fetch, push,
            ls-remote). Local-only git calls don't need this. When provided,
            overrides the inherited environment.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or get_settings().project_root),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def git_env_with_token(slug: str) -> dict[str, str] | None:
    """Build env dict with GIT_ASKPASS for a repo's scoped token.

    Returns None if no token is available (callers fall back to ambient
    credentials). Uses GIT_ASKPASS with a small inline script that echoes the
    token — safer than embedding tokens in URLs since the token never appears
    in .git/config or ``git remote -v`` output.
    """
    from pynchy.git_ops.repo import get_repo_token

    token = get_repo_token(slug)
    if not token:
        return None

    env = os.environ.copy()
    # GIT_ASKPASS is called with a prompt arg; we ignore it and always return
    # the token. Using printf avoids the token appearing in /proc/cmdline
    # (unlike echo in a temp script).
    env["GIT_ASKPASS"] = "/bin/sh"
    env["GIT_TERMINAL_PROMPT"] = "0"
    # The askpass "script" is /bin/sh, which reads from stdin... that doesn't
    # work. Instead, use a credential helper via environment:
    env["GH_TOKEN"] = token
    # gh auth git-credential respects GH_TOKEN. But for raw git operations
    # (not going through gh), we set up a minimal credential helper:
    env["GIT_CONFIG_COUNT"] = "2"
    env["GIT_CONFIG_KEY_0"] = "credential.https://github.com.username"
    env["GIT_CONFIG_VALUE_0"] = "x-access-token"
    env["GIT_CONFIG_KEY_1"] = "credential.https://github.com.helper"
    env["GIT_CONFIG_VALUE_1"] = (
        f"!f() {{ echo protocol=https; echo host=github.com; "
        f"echo username=x-access-token; echo password={token}; }}; f"
    )
    return env


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


def detect_main_branch(cwd: Path | None = None) -> str:
    """Detect the main branch name via origin/HEAD, fallback to 'main'."""
    result = run_git("symbolic-ref", "refs/remotes/origin/HEAD", cwd=cwd)
    if result.returncode == 0:
        # Output like "refs/remotes/origin/main"
        ref = result.stdout.strip()
        return ref.split("/")[-1]
    return "main"


def get_head_sha(cwd: Path | None = None) -> str:
    """Return the current git HEAD SHA, or 'unknown' on failure."""
    try:
        result = run_git("rev-parse", "HEAD", cwd=cwd)
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


def count_unpushed_commits(cwd: Path | None = None) -> int:
    """Count commits ahead of origin/main. Returns 0 on failure."""
    try:
        main = detect_main_branch(cwd=cwd)
        result = run_git("rev-list", f"origin/{main}..HEAD", "--count", cwd=cwd)
        if result.returncode == 0:
            return int(result.stdout.strip() or "0")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.debug("count_unpushed_commits failed", error=str(exc))
    return 0


def get_head_commit_message(max_length: int = 72, cwd: Path | None = None) -> str:
    """Return the subject line of the HEAD commit, truncated if needed."""
    try:
        result = run_git("log", "-1", "--format=%s", cwd=cwd)
        msg = result.stdout.strip() if result.returncode == 0 else ""
        if len(msg) > max_length:
            return msg[: max_length - 1] + "\u2026"
        return msg
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Failed to read HEAD commit message", err=str(exc))
        return ""


def files_changed_between(old_sha: str, new_sha: str, path: str) -> bool:
    """Check if files under *path* changed between two commits."""
    result = run_git("diff", "--name-only", old_sha, new_sha, "--", path)
    return bool(result.stdout.strip()) if result.returncode == 0 else False


def push_local_commits(
    *, skip_fetch: bool = False, cwd: Path | None = None, env: dict[str, str] | None = None
) -> bool:
    """Best-effort push of local commits to origin/main.

    Returns True if repo is in sync (nothing to push, or push succeeded).
    Retries once on rebase failure (covers the race where origin advances
    between fetch and rebase when two worktrees push nearly simultaneously).
    Never raises — all failures are logged and return False.

    Args:
        env: Optional environment for remote-facing git calls (fetch, push).
    """
    try:
        main = detect_main_branch(cwd=cwd)

        if not skip_fetch:
            fetch = run_git("fetch", "origin", cwd=cwd, env=env)
            if fetch.returncode != 0:
                logger.warning("push_local: git fetch failed", stderr=fetch.stderr.strip())
                return False

        count = run_git("rev-list", f"origin/{main}..HEAD", "--count", cwd=cwd)
        if count.returncode != 0 or int(count.stdout.strip() or "0") == 0:
            return True  # nothing to push (or can't tell)

        # Try rebase+push, retry once if origin advanced mid-operation
        for attempt in range(2):
            rebase = run_git("rebase", f"origin/{main}", cwd=cwd)
            if rebase.returncode != 0:
                run_git("rebase", "--abort", cwd=cwd)
                if attempt == 0:
                    # Re-fetch and retry — origin may have advanced
                    logger.info("push_local: rebase failed, retrying after fresh fetch")
                    retry_fetch = run_git("fetch", "origin", cwd=cwd, env=env)
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

            push = run_git("push", cwd=cwd, env=env)
            if push.returncode != 0:
                logger.warning("push_local: git push failed", stderr=push.stderr.strip())
                return False

            logger.info("push_local: pushed local commits")
            return True

        return False  # exhausted attempts
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.warning("push_local: unexpected error", err=str(exc))
        return False
