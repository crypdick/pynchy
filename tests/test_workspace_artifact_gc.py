"""Archived workspace artifacts leave safely through one public cleanup seam."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests build isolated temporary Git repositories.
from typing import TYPE_CHECKING

from pynchy.host.orchestrator.api import (
    cleanup_orphaned_workspace_artifacts,
    cleanup_workspace_artifacts,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed Git argv targets only the pytest temp directory.
        ["git", *args],  # noqa: S607 - test resolves the host Git executable.
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed Git argv targets only the pytest temp directory.
        ["git", *args],  # noqa: S607 - test resolves the host Git executable.
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def _managed_worktree(tmp_path: Path, folder: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".venv/\n")
    (repo / "README.md").write_text("test\n")
    _git(repo, "add", ".gitignore", "README.md")
    _git(repo, "commit", "-m", "initial")

    worktree = tmp_path / "worktrees" / "owner" / "repo" / folder
    worktree.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-b", f"worktree/{folder}", str(worktree))
    return repo, worktree


def _artifact_roots(tmp_path: Path, folder: str) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / "data"
    groups_dir = tmp_path / "groups"
    group_logs = groups_dir / folder / "logs"
    group_logs.mkdir(parents=True)
    (group_logs / "runtime.log").write_text("stale")
    for base in (data_dir / name for name in ("sessions", "ipc", "env", "approvals")):
        path = base / folder
        path.mkdir(parents=True)
        (path / "artifact").write_text("stale")
    return data_dir, groups_dir, tmp_path / "worktrees"


def test_cleanup_removes_clean_worktree_and_ephemeral_runtime_dirs(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-123"
    repo, worktree = _managed_worktree(tmp_path, folder)
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "ignored").write_text("cache")
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)

    assert cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )

    assert not worktree.exists()
    assert _git(repo, "show-ref", "--verify", f"refs/heads/worktree/{folder}").returncode == 0
    assert not (groups_dir / folder).exists()
    for name in ("sessions", "ipc", "env", "approvals"):
        assert not (data_dir / name / folder).exists()


def test_cleanup_retains_everything_when_worktree_has_uncommitted_work(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-456"
    _repo, worktree = _managed_worktree(tmp_path, folder)
    (worktree / "valuable.txt").write_text("unfinished")
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )

    assert worktree.is_dir()
    assert (groups_dir / folder / "logs" / "runtime.log").is_file()
    assert (data_dir / "sessions" / folder / "artifact").is_file()


def test_cleanup_retains_agent_workspace_files(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-789"
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)
    (groups_dir / folder / "notebook.ipynb").write_text("valuable")

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )

    assert (groups_dir / folder / "notebook.ipynb").is_file()
    assert (data_dir / "sessions" / folder / "artifact").is_file()


def test_cleanup_retains_artifacts_when_worktree_root_is_unsafe(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-987"
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)
    worktrees_dir.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )

    assert (groups_dir / folder / "logs" / "runtime.log").is_file()
    assert (data_dir / "sessions" / folder / "artifact").is_file()


def test_orphan_sweep_only_removes_unprotected_nonrouted_threads(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    groups_dir = tmp_path / "groups"
    worktrees_dir = tmp_path / "worktrees"
    orphan = "project__thread_discord-channel-123"
    protected = "project__thread_discord-channel-456"
    routed = "project__thread_conversation-conv_keep"
    static = "project"
    for folder in (orphan, protected, routed, static):
        (data_dir / "sessions" / folder).mkdir(parents=True)
        (groups_dir / folder).mkdir(parents=True)

    assert cleanup_orphaned_workspace_artifacts(
        {protected},
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    ) == [orphan]

    assert not (data_dir / "sessions" / orphan).exists()
    for folder in (protected, routed, static):
        assert (data_dir / "sessions" / folder).is_dir()
