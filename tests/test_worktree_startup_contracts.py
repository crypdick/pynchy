"""Startup and failure contracts for the public worktree provisioner."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests provide fixed git result objects.
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import pynchy.host.git_ops.worktree as worktree
from pynchy.host.git_ops.api import RepoContext, WorktreeError
from pynchy.host.git_ops.worktree import (
    WorktreeStartupRuntime,
    configure_worktree_startup_runtime,
    ensure_worktree,
    install_repo_hooks,
    reconcile_worktrees_at_startup,
)

if TYPE_CHECKING:
    from pathlib import Path


def _result(
    returncode: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


def _repo(tmp_path: Path) -> RepoContext:
    return RepoContext(
        slug="owner/project",
        root=tmp_path / "repo",
        worktrees_dir=tmp_path / "worktrees",
    )


def test_ensure_worktree_rejects_symbolic_link(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.worktrees_dir.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    (repo.worktrees_dir / "agent").symlink_to(target, target_is_directory=True)

    with pytest.raises(WorktreeError, match="symbolic link"):
        ensure_worktree("agent", repo)


def test_ensure_worktree_reports_fetch_failure(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with (
        patch.object(worktree, "detect_main_branch", return_value="main"),
        patch.object(worktree, "run_git", return_value=_result(1, stderr="offline")) as run,
        patch.object(worktree, "git_env_with_token", return_value={}),
        pytest.raises(WorktreeError, match="git fetch failed: offline"),
    ):
        ensure_worktree("agent", repo)

    run.assert_called_once_with("fetch", "origin", cwd=repo.root, env={})


def test_ensure_worktree_creates_new_branch_or_reattaches_existing_branch(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    cases = (
        (
            1,
            (
                "-b",
                "worktree/agent",
                str(repo.worktrees_dir / "agent"),
                "origin/main",
            ),
        ),
        (0, (str(repo.worktrees_dir / "agent"), "worktree/agent")),
    )
    for branch_status, expected_add in cases:
        with (
            patch.object(worktree, "detect_main_branch", return_value="main"),
            patch.object(
                worktree,
                "run_git",
                side_effect=[
                    _result(),
                    _result(),
                    _result(branch_status),
                    _result(),
                ],
            ) as run,
            patch.object(worktree, "git_env_with_token", return_value={}),
        ):
            result = ensure_worktree("agent", repo)

        assert result.path == repo.worktrees_dir / "agent"
        assert run.call_args_list[-1].args[0:2] == ("worktree", "add")
        assert run.call_args_list[-1].args[2:] == expected_add


def test_ensure_worktree_reports_branch_lookup_and_add_failures(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for branch_result, expected in (
        (_result(2, stderr="ref lookup failed"), "ref lookup failed"),
        (_result(1), "add failed"),
    ):
        add_result = _result(1, stderr="add failed")
        results = [_result(), _result(), branch_result]
        if branch_result.returncode == 1:
            results.append(add_result)
        with (
            patch.object(worktree, "detect_main_branch", return_value="main"),
            patch.object(worktree, "run_git", side_effect=results),
            patch.object(worktree, "git_env_with_token", return_value={}),
            pytest.raises(WorktreeError, match=expected),
        ):
            ensure_worktree("agent", repo)


def test_existing_worktree_preserves_dirty_state_and_reports_sync(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worktree_path = repo.worktrees_dir / "agent"
    worktree_path.mkdir(parents=True)
    with (
        patch.object(worktree, "detect_main_branch", return_value="main"),
        patch.object(
            worktree,
            "run_git",
            side_effect=[
                _result(),
                _result(stdout=" M file.py\n"),
                _result(),
                _result(stdout="before\n"),
                _result(),
                _result(stdout="after\n"),
            ],
        ),
        patch.object(worktree, "git_env_with_token", return_value={}),
    ):
        result = ensure_worktree("agent", repo)

    assert len(result.notices) == 2
    assert "uncommitted changes" in result.notices[0]
    assert "Auto-pulled remote changes" in result.notices[1]


def test_existing_worktree_reports_fetch_and_merge_failures(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worktree_path = repo.worktrees_dir / "agent"
    worktree_path.mkdir(parents=True)
    for fetch_result, expected in (
        (_result(1, stderr="network"), "fetch failed"),
        (_result(), "merge of origin/main failed"),
    ):
        results = [_result(), _result(), fetch_result]
        if fetch_result.returncode == 0:
            results.extend([_result(stdout="same"), _result(1, stderr="conflict")])
        with (
            patch.object(worktree, "detect_main_branch", return_value="main"),
            patch.object(worktree, "run_git", side_effect=results),
            patch.object(worktree, "git_env_with_token", return_value={}),
        ):
            result = ensure_worktree("agent", repo)

        assert len(result.notices) == 1
        assert expected in result.notices[0]


def test_install_repo_hooks_selects_declared_runner_and_handles_failure(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "prek.toml").write_text("[hooks]\n")
    with patch.object(worktree, "_run_hook_installer", return_value=_result()) as install:
        install_repo_hooks(repo_root)
    install.assert_called_once_with(repo_root, "prek")

    with patch.object(
        worktree,
        "_run_hook_installer",
        return_value=_result(1, stderr="broken"),
    ) as failed_install:
        install_repo_hooks(repo_root)
    failed_install.assert_called_once_with(repo_root, "prek")


def test_reconcile_startup_skips_missing_or_unavailable_repositories(tmp_path: Path) -> None:
    configure_worktree_startup_runtime(WorktreeStartupRuntime(tmp_path, tmp_path / "project", {}))
    with (
        patch.object(worktree.repo_manager, "get_repo_context", return_value=None),
        patch.object(worktree, "ensure_worktree") as ensure,
    ):
        reconcile_worktrees_at_startup({"owner/missing": ["agent"]})
    ensure.assert_not_called()

    repo = _repo(tmp_path)
    with (
        patch.object(worktree.repo_manager, "get_repo_context", return_value=repo),
        patch.object(worktree.repo_manager, "get_repo_token", return_value=None),
        patch.object(worktree.repo_manager, "ensure_repo_cloned", return_value=False) as clone,
        patch.object(worktree, "ensure_worktree") as ensure,
    ):
        reconcile_worktrees_at_startup({repo.slug: ["agent"]})
    clone.assert_called_once_with(repo)
    ensure.assert_not_called()
