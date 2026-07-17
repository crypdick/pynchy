"""Tests for bounded migration-backup retention."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from conftest import make_settings

from pynchy.__main__ import main
from pynchy.host.migration_backups import prune_migration_backups

if TYPE_CHECKING:
    from pathlib import Path


def _backup_dir(root: Path, name: str, mtime: int) -> Path:
    path = root / name
    path.mkdir()
    (path / "marker.txt").write_text(name)
    os.utime(path, (mtime, mtime))
    return path


def test_prune_migration_backups_keeps_newest_directories(tmp_path: Path) -> None:
    backups = tmp_path / "migration-backups"
    backups.mkdir()
    older = _backup_dir(backups, "20260705-runtime", 100)
    middle = _backup_dir(backups, "20260706-runtime", 200)
    newest = _backup_dir(backups, "20260707-runtime", 300)

    result = prune_migration_backups(backups, keep=2)

    assert result.kept == (newest, middle)
    assert result.removed == (older,)
    assert not older.exists()
    assert middle.exists()
    assert newest.exists()


def test_prune_migration_backups_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    backups = tmp_path / "migration-backups"
    backups.mkdir()
    older = _backup_dir(backups, "20260705-runtime", 100)
    newest = _backup_dir(backups, "20260707-runtime", 300)

    result = prune_migration_backups(backups, keep=1, dry_run=True)

    assert result.kept == (newest,)
    assert result.removed == (older,)
    assert older.exists()
    assert newest.exists()


def test_prune_migration_backups_ignores_files_and_symlinks(tmp_path: Path) -> None:
    backups = tmp_path / "migration-backups"
    backups.mkdir()
    older = _backup_dir(backups, "20260705-runtime", 100)
    newest = _backup_dir(backups, "20260707-runtime", 300)
    marker = backups / "README.txt"
    marker.write_text("not a backup directory")
    link = backups / "latest"
    link.symlink_to(newest, target_is_directory=True)
    broken_link = backups / "broken"
    broken_link.symlink_to(backups / "missing", target_is_directory=True)

    result = prune_migration_backups(backups, keep=1)

    assert result.kept == (newest,)
    assert result.removed == (older,)
    assert set(result.ignored) == {marker, broken_link, link}
    assert marker.exists()
    assert link.exists()
    assert broken_link.exists(follow_symlinks=False)


def test_prune_migration_backups_rejects_nonpositive_keep(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="keep must be at least 1"):
        prune_migration_backups(tmp_path, keep=0)


def test_cli_prune_migration_backups_defaults_to_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backups = tmp_path / "data" / "migration-backups"
    backups.mkdir(parents=True)
    older = _backup_dir(backups, "20260705-runtime", 100)
    _backup_dir(backups, "20260707-runtime", 300)

    monkeypatch.setattr(sys, "argv", ["pynchy", "prune-migration-backups", "--keep", "1"])
    monkeypatch.setattr(
        "pynchy.config.get_settings",
        lambda: make_settings(project_root=tmp_path),
    )

    main()

    assert older.exists()
    assert "Would remove" in capsys.readouterr().out
