"""RepoContext — abstraction for a tracked git repository.

Maps a GitHub slug (owner/repo) to its filesystem paths. Enables worktrees,
sync loops, and mount logic to work identically for pynchy's own repo and any
external repo configured under [repos."owner/repo"] in config.toml.
"""

from __future__ import annotations

import datetime
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
    """Resolve a slug to its RepoContext from [repos.*] config.

    Returns None if the slug is not listed under [repos.*].
    When path is omitted from config, the repo is auto-managed at data/repos/<owner>/<repo>/.
    """
    from pynchy.config import get_settings

    s = get_settings()
    repo_cfg = s.repos.get(slug)
    if repo_cfg is None:
        return None

    owner, repo_name = _slug_to_parts(slug)
    if repo_cfg.path is not None:
        root = Path(repo_cfg.path)  # already resolved by RepoConfig validator
    else:
        root = s.data_dir / "repos" / owner / repo_name
    worktrees_dir = s.worktrees_dir / owner / repo_name
    return RepoContext(slug=slug, root=root, worktrees_dir=worktrees_dir)


def get_repo_token(slug: str) -> str | None:
    """Resolve the git token for a repo, walking the fallback chain.

    Resolution order:
    1. repos."owner/repo".token — explicit per-repo token (highest priority)
    2. secrets.gh_token — host's broad token (fallback)
    3. gh auth token — auto-discovered from gh CLI (lowest priority)
    """
    from pynchy.config import get_settings
    from pynchy.host.container_manager.credentials import _read_gh_token

    s = get_settings()
    repo_cfg = s.repos.get(slug)
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

    Uses the repo's resolved token for authentication (supports private repos).
    After cloning, resets the remote URL to the bare form so the token doesn't
    persist in .git/config.
    """
    if repo_ctx.root.exists():
        return True

    repo_ctx.root.parent.mkdir(parents=True, exist_ok=True)

    token = get_repo_token(repo_ctx.slug)
    if token:
        clone_url = f"https://x-access-token:{token}@github.com/{repo_ctx.slug}"
    else:
        clone_url = f"https://github.com/{repo_ctx.slug}"

    logger.info("Cloning repo", slug=repo_ctx.slug, dest=str(repo_ctx.root))
    result = subprocess.run(
        ["git", "clone", clone_url, str(repo_ctx.root)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = _sanitize_token(result.stderr.strip(), token)
        logger.error("Failed to clone repo", slug=repo_ctx.slug, stderr=stderr)
        return False

    # Reset the remote URL to the bare form — token must not persist in .git/config.
    # Future fetch/push operations use the credential helper or env-based token.
    subprocess.run(
        ["git", "remote", "set-url", "origin", f"https://github.com/{repo_ctx.slug}"],
        cwd=str(repo_ctx.root),
        capture_output=True,
    )
    logger.info("Cloned repo", slug=repo_ctx.slug)
    return True


def resolve_repo_for_group(group_folder: str) -> RepoContext | None:
    """Look up workspace config.repo_access and return the resolved RepoContext.

    Returns None if the group has no repo_access or the slug is not configured.
    """
    from pynchy.config import get_settings

    s = get_settings()
    ws_cfg = s.workspaces.get(group_folder)
    if ws_cfg is None or not ws_cfg.repo_access:
        return None
    return get_repo_context(ws_cfg.repo_access)


def check_token_expiry(slug: str, token: str) -> None:
    """Check a fine-grained PAT's expiry via the GitHub API.

    Logs a warning if the token expires within _EXPIRY_WARNING_DAYS.
    Logs an error if the token is already expired.
    Silently succeeds if the API call fails (network issues, classic token, etc.).
    """
    try:
        # Use the /rate_limit endpoint with -i to get response headers
        # The github-authentication-token-expiration header reveals PAT expiry
        result = subprocess.run(
            [
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
        )
        if result.returncode != 0:
            return  # Can't check — might be a classic token or network issue

        # Parse github-authentication-token-expiration header
        for line in result.stdout.splitlines():
            if line.lower().startswith("github-authentication-token-expiration:"):
                expiry_str = line.split(":", 1)[1].strip()
                # Format: "2024-11-30 09:00:00 UTC"
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
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logger.debug("Could not check token expiry", slug=slug, err=str(exc))
