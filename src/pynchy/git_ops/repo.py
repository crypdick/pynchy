"""RepoContext — abstraction for a tracked git repository.

Maps a GitHub slug (owner/repo) to its filesystem paths. Enables worktrees,
sync loops, and mount logic to work identically for pynchy's own repo and any
external repo configured under [repos."owner/repo"] in config.toml.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from pynchy.logger import logger


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


def ensure_repo_cloned(repo_ctx: RepoContext) -> bool:
    """Clone the repo from GitHub if it doesn't exist yet.

    Only applies to auto-managed repos (those without an explicit path in config).
    Returns True if the repo root exists and is ready for worktree operations.
    """
    if repo_ctx.root.exists():
        return True

    repo_ctx.root.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning repo", slug=repo_ctx.slug, dest=str(repo_ctx.root))
    result = subprocess.run(
        ["git", "clone", f"https://github.com/{repo_ctx.slug}", str(repo_ctx.root)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Failed to clone repo", slug=repo_ctx.slug, stderr=result.stderr.strip())
        return False
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
