"""Utilities for bounded migration-backup retention."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - beartype resolves this runtime annotation.

DEFAULT_MIGRATION_BACKUP_KEEP = 3
_KEEP_TOO_SMALL_ERROR = "keep must be at least 1"


@dataclass(frozen=True)
class MigrationBackupPruneResult:
    kept: tuple[Path, ...]
    removed: tuple[Path, ...]
    ignored: tuple[Path, ...]


def _sort_newest_first(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda path: (path.lstat().st_mtime_ns, path.name), reverse=True)


def _validate_prune_request(backups_dir: Path, *, keep: int) -> MigrationBackupPruneResult | None:
    if keep < 1:
        raise ValueError(_KEEP_TOO_SMALL_ERROR)
    if not backups_dir.exists():
        return MigrationBackupPruneResult(kept=(), removed=(), ignored=())
    if not backups_dir.is_dir():
        raise NotADirectoryError(backups_dir)
    return None


def _partition_backup_entries(
    backups_dir: Path,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    entries = _sort_newest_first(list(backups_dir.iterdir()))
    candidates = tuple(path for path in entries if path.is_dir() and not path.is_symlink())
    ignored = tuple(path for path in entries if path not in candidates)
    return candidates, ignored


def _remove_backups(paths: tuple[Path, ...]) -> None:
    for path in paths:
        shutil.rmtree(path)


def prune_migration_backups(
    backups_dir: Path,
    *,
    keep: int = DEFAULT_MIGRATION_BACKUP_KEEP,
    dry_run: bool = False,
) -> MigrationBackupPruneResult:
    """Keep the newest migration backup directories and remove older ones."""
    early_result = _validate_prune_request(backups_dir, keep=keep)
    if early_result is not None:
        return early_result

    candidates, ignored = _partition_backup_entries(backups_dir)
    kept = candidates[:keep]
    removed = candidates[keep:]

    if not dry_run:
        _remove_backups(removed)

    return MigrationBackupPruneResult(kept=kept, removed=removed, ignored=ignored)
