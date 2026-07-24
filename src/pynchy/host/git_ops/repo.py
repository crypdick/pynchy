"""RepoContext — abstraction for a tracked git repository.

Maps a GitHub slug (owner/repo) to its filesystem paths. Enables worktrees,
sync loops, and mount logic to work identically for pynchy's own repo and any
external repo configured under [repos."owner/repo"] in layered settings.
"""

from __future__ import annotations

import datetime
import subprocess  # noqa: S404, RUF100 - repo helpers use fixed no-shell git/gh argv.
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import pynchy.config as pynchy_config
from pynchy.config.models import (
    ReposConfig,  # noqa: TC001, RUF100 - beartype resolves repo context settings at runtime.
)
from pynchy.logger import logger

# Warn when a token expires within this many days
_EXPIRY_WARNING_DAYS = 30


@dataclass(frozen=True)
class RepoContext:
    """All location info for a tracked git repository.

    Attributes:
        slug: GitHub slug, e.g. "crypdick/pynchy".
        root: Absolute path to the repository root on disk.
        worktrees_dir: Base directory for worktrees of this repo,
            i.e. data/worktrees/<owner>/<repo>/.
    """

    slug: str
    root: Path
    worktrees_dir: Path


def _slug_to_parts(slug: str) -> tuple[str, str]:
    """Split "owner/repo" into ("owner", "repo"). Raises ValueError if malformed."""
    parts = slug.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        msg = f"Invalid repo slug {slug!r}: expected 'owner/repo' format"
        raise ValueError(msg)
    return parts[0], parts[1]


def get_repo_context(slug: str) -> RepoContext | None:
    """Resolve a slug to its RepoContext.

    Per-repo overrides are optional. A profile can name ``repo = "owner/repo"``
    and Pynchy resolves the host checkout to ``repos.root / repo`` unless an
    explicit override supplies a different path or token. Container mounts
    remain owner-qualified via :func:`repo_container_path`.
    """
    s = pynchy_config.get_settings()

    owner, repo_name = _slug_to_parts(slug)
    root = repo_host_root(s, slug)
    if root is None:
        return None
    worktrees_dir = s.worktrees_dir / owner / repo_name
    return RepoContext(slug=slug, root=root, worktrees_dir=worktrees_dir)


@runtime_checkable
class _RepoSettings(Protocol):
    repos: ReposConfig


def repo_host_root(settings: _RepoSettings, slug: str) -> Path | None:
    """Return the host checkout path for a repo slug."""
    repo_cfg = settings.repos.overrides.get(slug)
    if repo_cfg and repo_cfg.path is not None:
        return Path(repo_cfg.path)
    try:
        _, repo_name = _slug_to_parts(slug)
    except ValueError:
        return None
    return Path(settings.repos.root) / repo_name


def repo_container_path(slug: str) -> str:
    """Return the stable in-container mount point for a repo slug."""
    owner, repo_name = _slug_to_parts(slug)
    return f"/workspace/repos/{owner}/{repo_name}"


def get_repo_token(slug: str) -> str | None:
    """Resolve the git token for a repo, trying each source in priority order.

    Resolution order:
    1. repos."owner/repo".token — explicit per-repo token (highest priority)
    2. secrets.gh_token — host's broad token (medium priority)
    3. gh auth token — auto-discovered from gh CLI (lowest priority)
    """
    from pynchy.host.container_manager.credentials import (  # noqa: PLC0415, RUF100 - importing container_manager.credentials at module load creates a git_ops/container_manager cycle.
        _read_gh_token,
    )

    s = pynchy_config.get_settings()
    repo_cfg = s.repos.overrides.get(slug)
    if repo_cfg and repo_cfg.token:
        return repo_cfg.token.get_secret_value()
    if s.secrets.gh_token:
        return s.secrets.gh_token.get_secret_value()
    return _read_gh_token()


def _sanitize_token(text: str, token: str | None) -> str:
    """Strip tokens from text to avoid leaking credentials in logs."""
    if token and token in text:
        return text.replace(token, "***")
    return text


def ensure_repo_cloned(repo_ctx: RepoContext) -> bool:
    """Clone the repo from GitHub if it doesn't exist yet.

    Only applies to auto-managed repos (those without an explicit path in config).
    Returns True if the repo root exists and is ready for worktree operations.

    Uses the repo's resolved git environment for authentication (supports
    private repos). The clone URL stays bare so tokens never appear in argv or
    persist in .git/config.
    """
    if repo_ctx.root.exists():
        return True

    repo_ctx.root.parent.mkdir(parents=True, exist_ok=True)

    from pynchy.host.git_ops.utils import (  # noqa: PLC0415, RUF100 - keep git_ops.utils dependency lazy during git_ops package initialization.
        git_env_with_token,
        run_git,
    )

    env = git_env_with_token(repo_ctx.slug)
    token = env.get("GH_TOKEN") if env else None
    clone_url = f"https://github.com/{repo_ctx.slug}"

    logger.info("Cloning repo", slug=repo_ctx.slug, dest=str(repo_ctx.root))
    result = run_git(
        "clone",
        clone_url,
        str(repo_ctx.root),
        cwd=repo_ctx.root.parent,
        env=env,
    )
    if result.returncode != 0:
        stderr = _sanitize_token(result.stderr.strip(), token)
        logger.error("Failed to clone repo", slug=repo_ctx.slug, stderr=stderr)
        return False

    # Keep the remote URL bare. Future fetch/push operations use env-based auth.
    subprocess.run(  # noqa: S603, RUF100 - remote URL is derived from repo slug and no shell is used.
        ["git", "remote", "set-url", "origin", f"https://github.com/{repo_ctx.slug}"],  # noqa: S607, RUF100 - git is the trusted host VCS executable.
        cwd=str(repo_ctx.root),
        capture_output=True,
        check=False,
    )
    logger.info("Cloned repo", slug=repo_ctx.slug)
    return True


def resolve_repo_for_group(group_folder: str) -> RepoContext | None:
    """Return the first resolved repo context for a workspace, if configured."""
    repo_contexts = resolve_repos_for_group(group_folder)
    return repo_contexts[0] if repo_contexts else None


def resolve_repos_for_group(group_folder: str) -> list[RepoContext]:
    """Return every resolved repo context for a workspace, preserving profile order."""
    from pynchy.host.orchestrator.workspace_config import (  # noqa: PLC0415, RUF100 - workspace config is orchestrator-owned and only needed for group resolution.
        load_resolved_config,
    )

    resolved = load_resolved_config(group_folder)
    if resolved is None or not resolved.repo:
        return []
    return [repo_ctx for slug in resolved.repo if (repo_ctx := get_repo_context(slug))]


def check_token_expiry(slug: str, token: str) -> None:
    """Check a fine-grained PAT's expiry via the GitHub API.

    Logs a warning if the token expires within _EXPIRY_WARNING_DAYS.
    Logs an error if the token is already expired.
    Silently succeeds if the API call fails (network issues, classic token, etc.).
    """
    try:
        result = subprocess.run(  # noqa: S603, RUF100 - fixed gh API argv with token passed as a header; no shell.
            [  # noqa: S607, RUF100 - gh is the trusted host GitHub CLI.
                "gh",
                "api",
                "/rate_limit",
                "-H",
                f"Authorization: token {token}",
                "-i",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logger.debug("Could not check token expiry", slug=slug, err=str(exc))
        return

    if result.returncode != 0:
        return  # Can't check — might be a classic token or network issue

    for line in result.stdout.splitlines():
        if line.lower().startswith("github-authentication-token-expiration:"):
            expiry_str = line.split(":", 1)[1].strip()
            # GitHub returns the expiry as a UTC timestamp, for example
            # "2024-11-30 09:00:00 UTC".
            expiry = datetime.datetime.strptime(expiry_str, "%Y-%m-%d %H:%M:%S %Z").replace(
                tzinfo=datetime.UTC
            )
            now = datetime.datetime.now(datetime.UTC)
            days_left = (expiry - now).days

            if days_left < 0:
                logger.error(
                    "Repo token has EXPIRED — git operations will fail",
                    slug=slug,
                    expired_on=expiry_str,
                )
            elif days_left <= _EXPIRY_WARNING_DAYS:
                logger.warning(
                    "Repo token expiring soon",
                    slug=slug,
                    expires=expiry_str,
                    days_left=days_left,
                )
            else:
                logger.debug(
                    "Repo token expiry OK",
                    slug=slug,
                    days_left=days_left,
                )
            return
