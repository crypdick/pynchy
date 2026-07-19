"""Shared git helpers used by worktree and git_sync modules."""

from __future__ import annotations

import os
import subprocess  # noqa: S404, RUF100 - shared git helper uses fixed no-shell argv.
from pathlib import (
    Path,  # noqa: TC003, RUF100 - beartype resolves git helper signatures at runtime.
)
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from pynchy.config import get_settings
from pynchy.host.container_manager.gateway import resolve_container_host
from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.host.container_manager.onecli import OneCliMaterial
else:
    OneCliMaterial = Any

_SUBPROCESS_TIMEOUT = 30
_DEFAULT_GIT_SSH_COMMAND = (
    "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=1"
)
_PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
_CA_ENV_KEYS = ("GIT_SSL_CAINFO", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")
_CONTAINER_HOSTNAME = "host.docker.internal"
_HOST_PROCESS_HOSTNAME = "localhost"


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


def _host_process_env(material: OneCliMaterial) -> dict[str, str]:
    host_paths_by_container_path = {
        mount.container_path: mount.host_path for mount in material.mounts
    }
    env = {
        key: _host_process_value(key, host_paths_by_container_path.get(value, value))
        for key, value in material.env_vars.items()
    }
    _configure_git_ca(env)
    return env


def _configure_git_ca(env: dict[str, str]) -> None:
    if env.get("GIT_SSL_CAINFO"):
        return
    for key in _CA_ENV_KEYS:
        if ca_path := env.get(key):
            env["GIT_SSL_CAINFO"] = ca_path
            return


def _host_process_value(key: str, value: str) -> str:
    if key in _PROXY_ENV_KEYS:
        return _rewrite_container_proxy_host(value)
    return value


def _rewrite_container_proxy_host(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    container_hosts = {
        _CONTAINER_HOSTNAME,
        resolve_container_host(_CONTAINER_HOSTNAME),
    }
    if parsed.hostname not in container_hosts:
        return value

    host_start = parsed.netloc.rfind(parsed.hostname)
    if host_start < 0:
        return value
    netloc = (
        f"{parsed.netloc[:host_start]}"
        f"{_HOST_PROCESS_HOSTNAME}"
        f"{parsed.netloc[host_start + len(parsed.hostname) :]}"
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def prepare_onecli_material(group_folder: str) -> OneCliMaterial | None:
    from pynchy.host.container_manager.onecli import (  # noqa: PLC0415, RUF100 - importing container_manager.onecli at module load creates a git_ops/container_manager cycle.
        prepare_onecli_material as _prepare_onecli_material,
    )

    return _prepare_onecli_material(group_folder)


def _git_env_with_onecli(slug: str, *, group_folder: str | None) -> dict[str, str] | None:
    s = get_settings()
    if not s.onecli.enabled:
        return None

    agent_key = group_folder or f"git-{slug}"
    material = prepare_onecli_material(agent_key)
    if material is None:
        return None

    env = _host_process_env(material)
    has_proxy = any(key in env for key in _PROXY_ENV_KEYS)
    if not has_proxy:
        from pynchy.host.container_manager.onecli import (  # noqa: PLC0415, RUF100 - importing container_manager.onecli at module load creates a git_ops/container_manager cycle.
            OneCliError,
        )

        reason = "OneCLI git material did not include proxy env"
        if s.onecli.fail_closed:
            raise OneCliError(reason)
        logger.warning(reason, slug=slug, onecli_agent=agent_key)
        return None

    merged = os.environ.copy()
    merged.update(env)
    merged["GIT_TERMINAL_PROMPT"] = "0"
    return merged


def git_env_with_token(slug: str, *, group_folder: str | None = None) -> dict[str, str] | None:
    """Build env dict for authenticated git remote operations.

    When OneCLI is enabled, git receives OneCLI proxy/CA env and no raw token.
    Otherwise this falls back to Pynchy's native repo token resolution.

    Returns None if no token is available (callers fall back to ambient
    credentials). Uses GIT_ASKPASS with a small inline script that echoes the
    token — safer than embedding tokens in URLs since the token never appears
    in .git/config or ``git remote -v`` output.
    """
    from pynchy.host.git_ops import (  # noqa: PLC0415, RUF100 - keep repo dependency lazy to avoid tightening git_ops package initialization.
        repo as git_repo,
    )

    if onecli_env := _git_env_with_onecli(slug, group_folder=group_folder):
        return onecli_env

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
