"""Utilities for bounded migration-backup retention."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MIGRATION_BACKUP_KEEP = 3


@dataclass(frozen=True)
class MigrationBackupPruneResult:
    kept: tuple[Path, ...]
    removed: tuple[Path, ...]
    ignored: tuple[Path, ...]


def _sort_newest_first(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda path: (path.lstat().st_mtime_ns, path.name), reverse=True)


def prune_migration_backups(
    backups_dir: Path,
    *,
    keep: int = DEFAULT_MIGRATION_BACKUP_KEEP,
    dry_run: bool = False,
) -> MigrationBackupPruneResult:
    """Keep the newest migration backup directories and remove older ones."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    if not backups_dir.exists():
        return MigrationBackupPruneResult(kept=(), removed=(), ignored=())
    if not backups_dir.is_dir():
        raise NotADirectoryError(backups_dir)

    entries = _sort_newest_first(list(backups_dir.iterdir()))
    candidates = tuple(path for path in entries if path.is_dir() and not path.is_symlink())
    ignored = tuple(path for path in entries if path not in candidates)
    kept = candidates[:keep]
    removed = candidates[keep:]

    if not dry_run:
        for path in removed:
            shutil.rmtree(path)

    return MigrationBackupPruneResult(kept=kept, removed=removed, ignored=ignored)
