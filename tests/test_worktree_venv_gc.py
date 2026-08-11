"""Managed-worktree virtual-environment retention."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from pynchy.host.git_ops.api import mark_worktree_used, prune_stale_worktree_venvs

if TYPE_CHECKING:
    from pathlib import Path

_DAY_SECONDS = 24 * 60 * 60


def _worktree(root: Path, group: str) -> Path:
    path = root / "owner" / "repo" / group
    path.mkdir(parents=True)
    return path


def _old(path: Path, *, now: float) -> None:
    timestamp = now - _DAY_SECONDS - 1
    os.utime(path, (timestamp, timestamp))


def test_prune_removes_stale_root_and_nested_venvs_only(tmp_path: Path) -> None:
    now = time.time()
    worktrees = tmp_path / "data" / "worktrees"
    stale = _worktree(worktrees, "stale")
    active = _worktree(worktrees, "active")
    fresh = _worktree(worktrees, "fresh")
    outside = tmp_path / ".venv"
    for path in (
        stale / ".venv",
        stale / "src" / "agent_runner" / ".venv",
        active / ".venv",
        fresh / ".venv",
        outside,
    ):
        path.mkdir(parents=True)
        _old(path, now=now)
    mark_worktree_used(fresh)

    removed = prune_stale_worktree_venvs(
        worktrees,
        active_folders={"active"},
        now=now,
    )

    assert removed == [stale / ".venv", stale / "src" / "agent_runner" / ".venv"]
    assert active.joinpath(".venv").is_dir()
    assert fresh.joinpath(".venv").is_dir()
    assert outside.is_dir()


def test_recent_use_touch_expires_after_24_hours(tmp_path: Path) -> None:
    now = time.time()
    worktrees = tmp_path / "worktrees"
    worktree = _worktree(worktrees, "agent")
    venv = worktree / ".venv"
    venv.mkdir()
    _old(venv, now=now)
    mark_worktree_used(worktree)

    assert prune_stale_worktree_venvs(worktrees, active_folders=set(), now=now) == []
    assert prune_stale_worktree_venvs(
        worktrees,
        active_folders=set(),
        now=now + _DAY_SECONDS + 1,
    ) == [venv]


def test_prune_leaves_symlinked_venv_untouched(tmp_path: Path) -> None:
    now = time.time()
    worktrees = tmp_path / "worktrees"
    worktree = _worktree(worktrees, "agent")
    target = tmp_path / "target"
    target.mkdir()
    venv = worktree / ".venv"
    venv.symlink_to(target, target_is_directory=True)

    assert prune_stale_worktree_venvs(worktrees, active_folders=set(), now=now) == []
    assert venv.is_symlink()
    assert target.is_dir()
