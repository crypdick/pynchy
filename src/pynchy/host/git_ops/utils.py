"""Shared git helpers used by worktree and git_sync modules."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess  # noqa: S404 - shared git helper uses fixed no-shell argv.
from collections.abc import Callable
from pathlib import (
    Path,
)

from pynchy.host.git_ops._environment import (  # noqa: F401 - preserve utility-module imports.
    git_env_with_token,
    git_env_without_credentials,
)
from pynchy.logger import logger

_SUBPROCESS_TIMEOUT = 30
_PROCESS_GROUP_TERMINATION_GRACE_SECONDS = 2
_DEFAULT_GIT_SSH_COMMAND = (
    "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ConnectionAttempts=1 "
    "-o ServerAliveInterval=5 -o ServerAliveCountMax=1"
)
_URL_USERINFO = re.compile(r"((?:https?|ssh)://)[^/\s@]+@")
_MAX_GIT_DIAGNOSTIC_LENGTH = 1000
_default_cwd: Path | None = None


def configure_git_default_cwd(project_root: Path) -> None:
    """Set the host checkout used only when a Git caller omits ``cwd``."""
    global _default_cwd  # noqa: PLW0603 - one host process owns one default Git checkout.
    _default_cwd = project_root


def _configured_default_cwd() -> Path:
    if _default_cwd is None:
        raise RuntimeError("Git default working directory has not been configured")
    return _default_cwd


def _git_subprocess_env(
    env: dict[str, str] | None,
    *,
    inherit_env: bool,
) -> dict[str, str]:
    """Return a noninteractive git environment with bounded SSH handshakes."""
    merged = os.environ.copy() if inherit_env else {}
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
    inherit_env: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with standard timeout and error capture.

    Args:
        env: Optional environment dict for remote-facing git calls (fetch, push,
            ls-remote). Local-only git calls don't need this. When provided,
            overrides the inherited environment.
    """
    command = ["git", *args]
    return _run_git_process(
        command,
        cwd=str(cwd or _configured_default_cwd()),
        env=_git_subprocess_env(env, inherit_env=inherit_env),
        timeout=timeout,
    )


def _run_git_process(
    command: list[str], *, cwd: str, env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run one git process while retaining ownership of its process group."""
    process = subprocess.Popen(  # noqa: S603 - git args are passed as argv by internal helper call sites; no shell.
        command,  # git is the trusted host VCS executable.
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        # Callers consistently branch on returncode. Preserve that contract so
        # a best-effort startup fetch cannot prevent the HTTP control plane
        # from coming up when GitHub is unavailable.
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout="",
            stderr=f"git command timed out after {timeout} seconds",
        )
    return subprocess.CompletedProcess(
        args=command,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate a timed-out git process and every child in its session."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.communicate(timeout=_PROCESS_GROUP_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()


def redact_git_diagnostic(text: str, *, token: str | None = None) -> str:
    """Return a bounded single-line git diagnostic with credentials redacted."""
    redacted = text.replace(token, "***") if token else text
    redacted = _URL_USERINFO.sub(r"\1***@", redacted)
    return " ".join(redacted.split())[:_MAX_GIT_DIAGNOSTIC_LENGTH]


def count_commits(
    range_expr: str,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    inherit_env: bool = True,
) -> int | None:
    """Count commits in a rev-list range (e.g. ``"main..branch"``).

    Returns ``None`` if the git command fails or its output can't be parsed,
    letting callers treat "couldn't determine" with a single ``is None`` guard.
    """
    result = run_git(
        "rev-list",
        range_expr,
        "--count",
        cwd=cwd,
        env=env,
        inherit_env=inherit_env,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return None


def detect_main_branch(
    cwd: Path | None = None,
    *,
    env: dict[str, str] | None = None,
    inherit_env: bool = True,
) -> str:
    """Detect the main branch name via origin/HEAD, defaulting to 'main'."""
    result = run_git(
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        cwd=cwd,
        env=env,
        inherit_env=inherit_env,
    )
    if result.returncode == 0:
        # Output like "refs/remotes/origin/main"
        ref = result.stdout.strip()
        return ref.removeprefix("refs/remotes/origin/")
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


def push_local_commits(  # noqa: PLR0913 - preserve the public Git-helper call shape.
    *,
    skip_fetch: bool = False,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    local_env: dict[str, str] | None = None,
    post_rebase_check: Callable[[], bool] | None = None,
    validated_source: Callable[[], str | None] | None = None,
    pre_push_check: Callable[[], bool] | None = None,
    include_diagnostics: bool = True,
    remote: str = "origin",
    fetch_refspec: str | None = None,
    main_branch: str | None = None,
    expected_head: str | None = None,
    inherit_env: bool = True,
) -> bool:
    """Best-effort push of local commits to origin/main.

    Returns True if repo is in sync (nothing to push, or push succeeded).
    Retries once on rebase failure (covers the race where origin advances
    between fetch and rebase when two worktrees push nearly simultaneously).
    Never raises — all failures are logged and return False.

    Args:
        env: Optional environment for remote-facing git calls (fetch, push).
        local_env: Optional credential-free environment for local Git calls.
            Defaults to an isolated environment with hooks disabled.
        post_rebase_check: Optional check to run after every successful rebase
            and before its push.
        validated_source: Optional callback returning the exact validated commit
            SHA to push after a rebase. This prevents a mutable ``HEAD`` from
            selecting different content after validation.
        pre_push_check: Optional check to run after source validation and
            immediately before push.
        include_diagnostics: Whether failure logs may include redacted Git output.
        remote: Remote name or validated URL used for fetch and push.
        fetch_refspec: Optional refspec used when fetching a URL instead of a remote name.
        main_branch: Optional already-validated target branch. When omitted, use
            the checkout's origin/HEAD as before.
        expected_head: Fail unless HEAD still names this validated commit before rebase.
    """
    if local_env is None:
        local_env = git_env_without_credentials()
    main = main_branch or _detect_main_branch_safe(
        cwd=cwd,
        env=local_env,
        include_diagnostics=include_diagnostics,
        inherit_env=False,
    )
    if main is None:
        return False

    if not skip_fetch:
        fetch = _fetch_git_remote(
            remote,
            fetch_refspec,
            cwd=cwd,
            env=env,
            inherit_env=inherit_env,
        )
        if fetch.returncode != 0:
            _log_push_failure(
                "push_local: git fetch failed",
                stderr=fetch.stderr,
                env=env,
                include_diagnostics=include_diagnostics,
            )
            return False

    if expected_head is not None and (
        _current_head(cwd=cwd, env=local_env, inherit_env=False) != expected_head
    ):
        logger.warning("push_local: validated HEAD changed before publication")
        return False

    ahead = count_commits(
        f"origin/{main}..HEAD",
        cwd=cwd,
        env=local_env,
        inherit_env=False,
    )
    if ahead is None:
        logger.warning("push_local: could not determine local commits ahead of origin")
        return False
    if ahead == 0:
        return True

    return _rebase_and_push_local_commits(
        main,
        cwd=cwd,
        local_env=local_env,
        remote_env=env,
        post_rebase_check=post_rebase_check,
        validated_source=validated_source,
        pre_push_check=pre_push_check,
        include_diagnostics=include_diagnostics,
        remote=remote,
        fetch_refspec=fetch_refspec,
        expected_head=expected_head,
        inherit_env=inherit_env,
    )


def _detect_main_branch_safe(
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    include_diagnostics: bool,
    inherit_env: bool,
) -> str | None:
    try:
        return detect_main_branch(cwd=cwd, env=env, inherit_env=inherit_env)
    except (OSError, subprocess.SubprocessError) as exc:
        _log_push_failure(
            "push_local: unexpected error",
            stderr=str(exc),
            env=None,
            include_diagnostics=include_diagnostics,
        )
        return None


def _rebase_and_push_local_commits(  # noqa: PLR0911, PLR0913 - bounded retry states need distinct exits.
    main: str,
    *,
    cwd: Path | None,
    local_env: dict[str, str] | None,
    remote_env: dict[str, str] | None,
    post_rebase_check: Callable[[], bool] | None,
    validated_source: Callable[[], str | None] | None,
    pre_push_check: Callable[[], bool] | None,
    include_diagnostics: bool,
    remote: str,
    fetch_refspec: str | None,
    expected_head: str | None,
    inherit_env: bool,
) -> bool:
    # Try rebase+push, retry once if origin advanced mid-operation.
    attempt = 0
    while True:
        if expected_head is not None and (
            _current_head(cwd=cwd, env=local_env, inherit_env=False) != expected_head
        ):
            logger.warning("push_local: validated HEAD changed before rebase")
            return False
        rebase = run_git(
            "rebase",
            f"origin/{main}",
            cwd=cwd,
            env=local_env,
            inherit_env=False,
        )
        if rebase.returncode != 0:
            run_git("rebase", "--abort", cwd=cwd, env=local_env, inherit_env=False)
            if attempt == 0:
                logger.info("push_local: rebase failed, retrying after fresh fetch")
                retry_fetch = _fetch_git_remote(
                    remote,
                    fetch_refspec,
                    cwd=cwd,
                    env=remote_env,
                    inherit_env=inherit_env,
                )
                if retry_fetch.returncode != 0:
                    _log_push_failure(
                        "push_local: retry fetch failed",
                        stderr=retry_fetch.stderr,
                        env=remote_env,
                        include_diagnostics=include_diagnostics,
                    )
                    return False
                attempt = 1
                continue
            _log_push_failure(
                "push_local: rebase failed after retry",
                stderr=rebase.stderr,
                env=local_env,
                include_diagnostics=include_diagnostics,
            )
            return False

        if not _post_rebase_check_succeeds(post_rebase_check):
            return False

        source = _validated_source(validated_source)
        if validated_source is not None and source is None:
            return False
        if not _post_rebase_check_succeeds(pre_push_check):
            return False

        push = _push_git_remote(
            remote,
            main,
            cwd=cwd,
            env=remote_env,
            inherit_env=inherit_env,
            source=source,
        )
        if push.returncode != 0:
            _log_push_failure(
                "push_local: git push failed",
                stderr=push.stderr,
                env=remote_env,
                include_diagnostics=include_diagnostics,
            )
            return False
        logger.info("push_local: pushed local commits")
        return True


def _fetch_git_remote(
    remote: str,
    refspec: str | None,
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    inherit_env: bool,
) -> subprocess.CompletedProcess[str]:
    args = ("fetch", remote) if refspec is None else ("fetch", remote, refspec)
    return run_git(*args, cwd=cwd, env=env, inherit_env=inherit_env)


def _current_head(
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    inherit_env: bool,
) -> str | None:
    head = run_git(
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        cwd=cwd,
        env=env,
        inherit_env=inherit_env,
    )
    return head.stdout.strip() if head.returncode == 0 and head.stdout.strip() else None


def _push_git_remote(  # noqa: PLR0913 - remote call requires its explicit trust inputs.
    remote: str,
    main: str,
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    inherit_env: bool,
    source: str | None,
) -> subprocess.CompletedProcess[str]:
    if source is None:
        return run_git("push", remote, cwd=cwd, env=env, inherit_env=inherit_env)
    return run_git(
        "push",
        "--no-verify",
        remote,
        f"{source}:refs/heads/{main}",
        cwd=cwd,
        env=env,
        inherit_env=inherit_env,
    )


def _post_rebase_check_succeeds(check: Callable[[], bool] | None) -> bool:
    if check is None:
        return True
    try:
        valid = check()
    except (OSError, ValueError):
        valid = False
    if not valid:
        logger.warning("push_local: post-rebase validation failed")
    return valid


def _validated_source(check: Callable[[], str | None] | None) -> str | None:
    """Return a post-rebase commit selected by a validation callback."""
    if check is None:
        return None
    try:
        source = check()
    except (OSError, ValueError):
        source = None
    if source is None:
        logger.warning("push_local: post-rebase validation failed")
    return source


def _log_push_failure(
    message: str,
    *,
    stderr: str,
    env: dict[str, str] | None,
    include_diagnostics: bool,
) -> None:
    if not include_diagnostics or not stderr:
        logger.warning(message)
        return
    logger.warning(
        message,
        stderr=redact_git_diagnostic(stderr, token=env.get("GH_TOKEN") if env else None),
    )
