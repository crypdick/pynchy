"""Retention for virtual environments inside Pynchy-managed worktrees."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from pynchy.logger import logger

_RETENTION_SECONDS = 24 * 60 * 60


def mark_worktree_used(worktree: Path) -> None:
    """Record actual agent use without relying on filesystem access times."""
    if worktree.is_symlink() or not worktree.is_dir():
        return
    try:
        venvs = (
            path for path in worktree.rglob(".venv") if path.is_dir() and not path.is_symlink()
        )
        for venv in venvs:
            os.utime(venv)
    except OSError as exc:
        logger.warning("Could not mark worktree as used", path=str(worktree), error=str(exc))


def _managed_worktrees(root: Path) -> list[Path]:
    """Return only owner/repo/group directories below the managed root."""
    if not root.is_dir():
        return []
    try:
        return sorted(
            group
            for owner in root.iterdir()
            if owner.is_dir() and not owner.is_symlink()
            for repo in owner.iterdir()
            if repo.is_dir() and not repo.is_symlink()
            for group in repo.iterdir()
            if group.is_dir() and not group.is_symlink()
        )
    except OSError as exc:
        logger.warning("Could not scan managed worktrees", path=str(root), error=str(exc))
        return []


def prune_stale_worktree_venvs(
    worktrees_root: Path,
    *,
    active_folders: set[str],
    now: float | None = None,
) -> list[Path]:
    """Remove managed-worktree ``.venv`` directories after 24 hours without use."""
    cutoff = (time.time() if now is None else now) - _RETENTION_SECONDS
    removed: list[Path] = []
    for worktree in _managed_worktrees(worktrees_root):
        if worktree.name in active_folders:
            continue
        try:
            venvs = sorted(
                path for path in worktree.rglob(".venv") if path.is_dir() and not path.is_symlink()
            )
        except OSError as exc:
            logger.warning("Could not inspect worktree venvs", path=str(worktree), error=str(exc))
            continue
        for venv in venvs:
            try:
                if venv.stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(venv)
            except OSError as exc:
                logger.warning(
                    "Could not remove stale worktree venv",
                    path=str(venv),
                    error=str(exc),
                )
                continue
            removed.append(venv)
    if removed:
        logger.info("Pruned stale worktree venvs", count=len(removed))
    return removed
