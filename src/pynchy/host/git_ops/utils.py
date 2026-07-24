"""Shared git helpers used by worktree and git_sync modules."""

from __future__ import annotations

import os
import re
import subprocess  # noqa: S404, RUF100 - shared git helper uses fixed no-shell argv.
from pathlib import (
    Path,  # noqa: TC003, RUF100 - beartype resolves git helper signatures at runtime.
)

from pynchy.config import get_settings
from pynchy.logger import logger

_SUBPROCESS_TIMEOUT = 30
_DEFAULT_GIT_SSH_COMMAND = (
    "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=1"
)
_URL_USERINFO = re.compile(r"(https?://)[^/\s@]+@")
_MAX_GIT_DIAGNOSTIC_LENGTH = 1000


def _git_subprocess_env(env: dict[str, str] | None) -> dict[str, str]:
    """Return a noninteractive git environment with bounded SSH handshakes."""
    merged = os.environ.copy()
    if env:
        merged.update(env)
    merged.setdefault("GIT_TERMINAL_PROMPT", "0")
    merged.setdefault("GIT_SSH_COMMAND", _DEFAULT_GIT_SSH_COMMAND)
    return merged


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
    command = ["git", *args]
    try:
        return subprocess.run(  # noqa: S603, RUF100 - git args are passed as argv by internal helper call sites; no shell.
            command,  # noqa: S607, RUF100 - git is the trusted host VCS executable.
            cwd=str(cwd or get_settings().project_root),
            capture_output=True,
            text=True,
            start_new_session=True,
            env=_git_subprocess_env(env),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Callers consistently branch on returncode.  Preserve that contract so
        # a best-effort startup fetch cannot prevent the HTTP control plane
        # from coming up when GitHub is unavailable.
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout="",
            stderr=f"git command timed out after {timeout} seconds",
        )


def redact_git_diagnostic(text: str, *, token: str | None = None) -> str:
    """Return a bounded single-line git diagnostic with credentials redacted."""
    redacted = text.replace(token, "***") if token else text
    redacted = _URL_USERINFO.sub(r"\1***@", redacted)
    return " ".join(redacted.split())[:_MAX_GIT_DIAGNOSTIC_LENGTH]


def git_env_with_token(slug: str) -> dict[str, str] | None:
    """Build env dict for authenticated git remote operations.

    Returns None if no token is available (callers fall back to ambient
    credentials). Uses GIT_ASKPASS with a small inline script that echoes the
    token — safer than embedding tokens in URLs since the token never appears
    in .git/config or ``git remote -v`` output.
    """
    from pynchy.host.git_ops import (  # noqa: PLC0415, RUF100 - keep repo dependency lazy to avoid tightening git_ops package initialization.
        repo as git_repo,
    )

    token = git_repo.get_repo_token(slug)
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


def count_commits(range_expr: str, *, cwd: Path | None = None) -> int | None:
    """Count commits in a rev-list range (e.g. ``"main..branch"``).

    Returns ``None`` if the git command fails or its output can't be parsed,
    letting callers treat "couldn't determine" with a single ``is None`` guard.
    """
    result = run_git("rev-list", range_expr, "--count", cwd=cwd)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return None


def detect_main_branch(cwd: Path | None = None) -> str:
    """Detect the main branch name via origin/HEAD, defaulting to 'main'."""
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
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("count_unpushed_commits failed", error=str(exc))
        return 0
    return count_commits(f"origin/{main}..HEAD", cwd=cwd) or 0


def get_head_commit_message(max_length: int = 72, cwd: Path | None = None) -> str:
    """Return the subject line of the HEAD commit, truncated if needed."""
    try:
        result = run_git("log", "-1", "--format=%s", cwd=cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Failed to read HEAD commit message", err=str(exc))
        return ""
    else:
        msg = result.stdout.strip() if result.returncode == 0 else ""
        if len(msg) > max_length:
            return msg[: max_length - 1] + "\u2026"
        return msg


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
    main = _detect_main_branch_safe(cwd=cwd)
    if main is None:
        return False

    if not skip_fetch:
        fetch = run_git("fetch", "origin", cwd=cwd, env=env)
        if fetch.returncode != 0:
            logger.warning("push_local: git fetch failed", stderr=fetch.stderr.strip())
            return False

    ahead = count_commits(f"origin/{main}..HEAD", cwd=cwd)
    if not ahead:
        return True  # nothing to push (or can't tell)

    return _rebase_and_push_local_commits(main, cwd=cwd, env=env)


def _detect_main_branch_safe(cwd: Path | None) -> str | None:
    try:
        return detect_main_branch(cwd=cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("push_local: unexpected error", err=str(exc))
        return None


def _rebase_and_push_local_commits(
    main: str, *, cwd: Path | None, env: dict[str, str] | None
) -> bool:
    # Try rebase+push, retry once if origin advanced mid-operation.
    for attempt in range(2):
        rebase = run_git("rebase", f"origin/{main}", cwd=cwd)
        if rebase.returncode != 0:
            run_git("rebase", "--abort", cwd=cwd)
            if attempt == 0:
                logger.info("push_local: rebase failed, retrying after fresh fetch")
                retry_fetch = run_git("fetch", "origin", cwd=cwd, env=env)
                if retry_fetch.returncode != 0:
                    logger.warning(
                        "push_local: retry fetch failed", stderr=retry_fetch.stderr.strip()
                    )
                    return False
                continue
            logger.warning("push_local: rebase failed after retry", stderr=rebase.stderr.strip())
            return False

        push = run_git("push", cwd=cwd, env=env)
        if push.returncode != 0:
            logger.warning("push_local: git push failed", stderr=push.stderr.strip())
            return False
        logger.info("push_local: pushed local commits")
        return True
    return False
