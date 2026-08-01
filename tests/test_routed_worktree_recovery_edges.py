"""Recovery and fail-closed edges for routed host worktree selection."""

from __future__ import annotations

import subprocess  # noqa: S404 - test helper runs fixed Git argv.
from contextlib import ExitStack
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.host.git_ops.api import (
    RepoContext,
    RoutedHostWorktreeError,
    resolve_routed_host_worktree_cwd,
    run_git,
    select_routed_host_repo,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed test-only Git argv.
        ["/usr/bin/git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def git_env(tmp_path: Path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")
    setup = tmp_path / "setup"
    _git(tmp_path, "clone", str(origin), str(setup))
    _git(setup, "config", "user.email", "test@test.com")
    _git(setup, "config", "user.name", "Test")
    (setup / "README.md").write_text("initial")
    _git(setup, "add", "README.md")
    _git(setup, "commit", "-m", "initial commit")
    _git(setup, "push", "origin", "main")
    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))
    _git(project, "config", "user.email", "test@test.com")
    _git(project, "config", "user.name", "Test")
    worktrees_dir = tmp_path / "worktrees"
    settings = make_settings(project_root=project, worktrees_dir=worktrees_dir)
    repo_ctx = RepoContext("owner/pynchy", project, worktrees_dir)
    with ExitStack() as stack:
        stack.enter_context(patch("pynchy.host.git_ops.utils._default_cwd", project))
        stack.enter_context(patch("pynchy.config.api.get_settings", return_value=settings))
        stack.enter_context(patch("pynchy.host.git_ops.repo.ensure_repo_cloned", return_value=True))
        yield {"project": project, "worktrees_dir": worktrees_dir, "repo_ctx": repo_ctx}


def test_selection_fails_closed_when_git_identity_is_invalid(git_env: dict):
    with (
        patch(
            "pynchy.host.git_ops._routed_host_worktree.run_git",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="invalid"),
        ),
        pytest.raises(RoutedHostWorktreeError, match="Could not verify"),
    ):
        select_routed_host_repo(git_env["project"], [git_env["repo_ctx"]])


def test_selection_fails_closed_when_git_identity_paths_cannot_be_resolved(git_env: dict):
    valid = subprocess.CompletedProcess([], 0, stdout=str(git_env["project"]), stderr="")
    with (
        patch(
            "pynchy.host.git_ops._routed_host_worktree.run_git",
            return_value=valid,
        ),
        patch(
            "pynchy.host.git_ops._routed_host_worktree.Path.resolve",
            side_effect=OSError("path unavailable"),
        ),
        pytest.raises(RoutedHostWorktreeError, match="Could not resolve"),
    ):
        select_routed_host_repo(git_env["project"], [git_env["repo_ctx"]])


def test_clean_legacy_source_can_recover_into_a_child_worktree(git_env: dict):
    folder = "host__thread_conversation-conv_clean_recovery"

    result = resolve_routed_host_worktree_cwd(
        folder,
        git_env["project"],
        [git_env["repo_ctx"]],
        recovered=True,
    )

    assert result.cwd == git_env["worktrees_dir"] / folder


def test_recovery_rejects_unverifiable_legacy_source_status(git_env: dict):
    original_run_git = run_git

    def failing_status(
        command: str, *args: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command == "status":
            return subprocess.CompletedProcess([], 1, stdout="", stderr="failed")
        return original_run_git(command, *args, **kwargs)

    with (
        patch(
            "pynchy.host.git_ops._routed_host_worktree.run_git",
            side_effect=failing_status,
        ),
        pytest.raises(RoutedHostWorktreeError, match="Could not verify"),
    ):
        resolve_routed_host_worktree_cwd(
            "host__thread_conversation-conv_status_failure",
            git_env["project"],
            [git_env["repo_ctx"]],
            recovered=True,
        )


def test_recovery_rejects_legacy_source_when_status_cannot_be_read(git_env: dict):
    original_run_git = run_git

    def unreadable_status(
        command: str, *args: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command == "status":
            raise OSError("status unavailable")
        return original_run_git(command, *args, **kwargs)

    with (
        patch(
            "pynchy.host.git_ops._routed_host_worktree.run_git",
            side_effect=unreadable_status,
        ),
        pytest.raises(RoutedHostWorktreeError, match="Could not verify"),
    ):
        resolve_routed_host_worktree_cwd(
            "host__thread_conversation-conv_status_unreadable",
            git_env["project"],
            [git_env["repo_ctx"]],
            recovered=True,
        )


def test_recovery_rejects_legacy_source_when_main_branch_cannot_be_read(
    git_env: dict,
):
    with (
        patch(
            "pynchy.host.git_ops._routed_host_worktree.detect_main_branch",
            side_effect=OSError("branch unavailable"),
        ),
        pytest.raises(RoutedHostWorktreeError, match="ahead of its main branch"),
    ):
        resolve_routed_host_worktree_cwd(
            "host__thread_conversation-conv_branch_failure",
            git_env["project"],
            [git_env["repo_ctx"]],
            recovered=True,
        )


def test_recovery_rejects_legacy_source_when_ahead_count_is_unknown(git_env: dict):
    with (
        patch(
            "pynchy.host.git_ops._routed_host_worktree.count_commits",
            return_value=None,
        ),
        pytest.raises(RoutedHostWorktreeError, match="ahead of its main branch"),
    ):
        resolve_routed_host_worktree_cwd(
            "host__thread_conversation-conv_unknown_ahead",
            git_env["project"],
            [git_env["repo_ctx"]],
            recovered=True,
        )
