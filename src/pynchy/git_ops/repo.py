"""RepoContext — abstraction for a tracked git repository.

Maps a GitHub slug (owner/repo) to its filesystem paths. Enables worktrees,
sync loops, and mount logic to work identically for pynchy's own repo and any
external repo configured under [repos."owner/repo"] in config.toml.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
    """
    from pynchy.config import get_settings

    s = get_settings()
    repo_cfg = s.repos.get(slug)
    if repo_cfg is None:
        return None

    owner, repo_name = _slug_to_parts(slug)
    root = Path(repo_cfg.path)  # already resolved by RepoConfig validator
    worktrees_dir = s.worktrees_dir / owner / repo_name
    return RepoContext(slug=slug, root=root, worktrees_dir=worktrees_dir)


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
