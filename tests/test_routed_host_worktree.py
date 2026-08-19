"""Tests for routed host worktree selection and provisioning."""

from __future__ import annotations

import fcntl
import os
import subprocess  # noqa: S404 - test helper runs git in temporary repositories
from contextlib import ExitStack
from threading import Event, Thread, current_thread
from typing import IO, TYPE_CHECKING
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.host.git_ops.api import (
    RepoContext,
    RoutedHostWorktreeError,
    RoutedHostWorktreeResult,
    WorktreeResult,
    resolve_routed_host_worktree_cwd,
    select_routed_host_repo,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test helper runs fixed git argv against temp repos
        ["git", *args],  # noqa: S607 - test helper deliberately resolves git from PATH
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_bare_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")

    clone = tmp_path / "setup-clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@test.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "README.md").write_text("initial")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-m", "initial commit")
    _git(clone, "push", "origin", "main")
    return origin


@pytest.fixture
def git_env(tmp_path: Path):
    origin = _make_bare_origin(tmp_path)
    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))
    _git(project, "config", "user.email", "test@test.com")
    _git(project, "config", "user.name", "Test")
    worktrees_dir = tmp_path / "worktrees"
    settings = make_settings(project_root=project, worktrees_dir=worktrees_dir)
    repo_ctx = RepoContext(slug="owner/pynchy", root=project, worktrees_dir=worktrees_dir)

    with ExitStack() as stack:
        stack.enter_context(patch("pynchy.host.git_ops.utils._default_cwd", settings.project_root))
        stack.enter_context(patch("pynchy.config.api.get_settings", return_value=settings))
        stack.enter_context(patch("pynchy.host.git_ops.repo.ensure_repo_cloned", return_value=True))
        yield {
            "origin": origin,
            "project": project,
            "worktrees_dir": worktrees_dir,
            "repo_ctx": repo_ctx,
        }


class TestRoutedHostWorktrees:
    @pytest.mark.parametrize("kind", ["symlink", "fifo"])
    def test_rejects_unsafe_routed_worktree_lock_file(self, git_env: dict, kind: str):
        lock_path = git_env["project"] / ".git" / "pynchy-routed-worktree.lock"
        if kind == "symlink":
            lock_path.symlink_to(git_env["project"] / "README.md")
        else:
            os.mkfifo(lock_path)

        with pytest.raises(RoutedHostWorktreeError, match="Could not lock"):
            resolve_routed_host_worktree_cwd(
                "host__thread_conversation-conv_unsafe-lock",
                git_env["project"],
                [git_env["repo_ctx"]],
                recovered=False,
            )

    def test_lock_failure_closes_file_and_maps_error(self, git_env: dict):
        opened_files: list[IO[str]] = []
        original_fdopen = os.fdopen

        def capture_fdopen(fd: int, mode: str, *, encoding: str) -> IO[str]:
            lock_file = original_fdopen(fd, mode, encoding=encoding)
            opened_files.append(lock_file)
            return lock_file

        with (
            patch(
                "pynchy.host.git_ops._routed_host_worktree.os.fdopen",
                side_effect=capture_fdopen,
            ),
            patch(
                "pynchy.host.git_ops._routed_host_worktree.fcntl.flock",
                side_effect=OSError("lock failed"),
            ),
            pytest.raises(RoutedHostWorktreeError, match="Could not lock") as exc_info,
        ):
            resolve_routed_host_worktree_cwd(
                "host__thread_conversation-conv_lock-failure",
                git_env["project"],
                [git_env["repo_ctx"]],
                recovered=False,
            )

        assert isinstance(exc_info.value.__cause__, OSError)
        assert len(opened_files) == 1
        assert opened_files[0].closed

    def test_serializes_provisioning_for_routes_in_one_repository(self, git_env: dict):
        """Two routed children must not mutate one Git repository concurrently."""
        repo_ctx = git_env["repo_ctx"]
        first_folder = "host__thread_conversation-conv_first"
        second_folder = "host__thread_conversation-conv_second"
        first_entered = Event()
        release_first = Event()
        second_lock_attempted = Event()
        second_entered = Event()
        results: list[RoutedHostWorktreeResult] = []
        thread_errors: list[Exception] = []
        original_flock = fcntl.flock

        def tracked_flock(fd: int, operation: int) -> None:
            if current_thread().name == "routed-second-contender":
                second_lock_attempted.set()
            original_flock(fd, operation)

        def provision(folder: str, repo: RepoContext) -> WorktreeResult:
            if folder == first_folder:
                first_entered.set()
                assert release_first.wait(timeout=2)
            else:
                second_entered.set()
            path = repo.worktrees_dir / folder
            path.mkdir(parents=True)
            return WorktreeResult(path)

        def resolve(folder: str) -> None:
            try:
                results.append(
                    resolve_routed_host_worktree_cwd(
                        folder,
                        git_env["project"],
                        [repo_ctx],
                        recovered=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001  # allow: exception-handling; thread result
                thread_errors.append(exc)

        with (
            patch("pynchy.host.git_ops.worktree.ensure_worktree", side_effect=provision),
            patch(
                "pynchy.host.git_ops._routed_host_worktree.fcntl.flock",
                side_effect=tracked_flock,
            ),
        ):
            first = Thread(target=resolve, args=(first_folder,))
            second = Thread(
                target=resolve,
                args=(second_folder,),
                name="routed-second-contender",
            )
            first.start()
            assert first_entered.wait(timeout=2)
            second.start()
            try:
                assert second_lock_attempted.wait(timeout=2)
                assert not second_entered.is_set()
            finally:
                release_first.set()
                first.join(timeout=2)
                second.join(timeout=2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert thread_errors == []
        assert {result.cwd.name for result in results} == {first_folder, second_folder}

    def test_selects_repository_that_owns_source_checkout(self, git_env: dict):
        assert (
            select_routed_host_repo(git_env["project"], [git_env["repo_ctx"]])
            is git_env["repo_ctx"]
        )

    def test_rejects_routed_slot_occupied_by_a_file(self, git_env: dict):
        folder = "host__thread_conversation-conv_file_slot"
        worktree_path = git_env["worktrees_dir"] / folder
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        worktree_path.write_text("do not replace\n")

        with pytest.raises(RoutedHostWorktreeError, match="non-directory"):
            resolve_routed_host_worktree_cwd(
                folder,
                git_env["project"],
                [git_env["repo_ctx"]],
                recovered=False,
            )

        assert worktree_path.read_text() == "do not replace\n"

    def test_selection_fails_closed_when_git_inspection_errors(self, git_env: dict):
        with (
            patch(
                "pynchy.host.git_ops._routed_host_worktree.run_git",
                side_effect=OSError("git unavailable"),
            ),
            pytest.raises(RoutedHostWorktreeError, match="Could not inspect"),
        ):
            select_routed_host_repo(git_env["project"], [git_env["repo_ctx"]])

    def test_provisioning_failure_is_reported_as_routed_worktree_error(self, git_env: dict):
        with (
            patch(
                "pynchy.host.git_ops.worktree.ensure_worktree",
                side_effect=OSError("worktree unavailable"),
            ),
            pytest.raises(RoutedHostWorktreeError, match="Could not prepare"),
        ):
            resolve_routed_host_worktree_cwd(
                "host__thread_conversation-conv_failure",
                git_env["project"],
                [git_env["repo_ctx"]],
                recovered=False,
            )

    def test_missing_mapped_subdirectory_is_rejected(self, git_env: dict, tmp_path: Path):
        source_cwd = git_env["project"] / "tools"
        source_cwd.mkdir()
        replacement = tmp_path / "replacement-worktree"
        replacement.mkdir()

        with (
            patch(
                "pynchy.host.git_ops.worktree.ensure_worktree",
                return_value=WorktreeResult(replacement),
            ),
            pytest.raises(RoutedHostWorktreeError, match="unavailable"),
        ):
            resolve_routed_host_worktree_cwd(
                "host__thread_conversation-conv_missing-subdirectory",
                source_cwd,
                [git_env["repo_ctx"]],
                recovered=False,
            )

    def test_maps_relative_cwd_and_reuses_child_worktree_after_recovery(self, git_env: dict):
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]
        source_cwd = project / "tools"
        source_cwd.mkdir()
        (source_cwd / "runner.txt").write_text("tracked")
        _git(project, "add", "tools/runner.txt")
        _git(project, "commit", "-m", "add host tools")
        _git(project, "push", "origin", "main")
        folder = "host__thread_conversation-conv_recovery"

        first = resolve_routed_host_worktree_cwd(
            folder,
            source_cwd,
            [repo_ctx],
            recovered=False,
        )
        second = resolve_routed_host_worktree_cwd(
            folder,
            source_cwd,
            [repo_ctx],
            recovered=True,
        )

        assert first.cwd == git_env["worktrees_dir"] / folder / "tools"
        assert second.cwd == first.cwd
        assert _git(first.cwd, "branch", "--show-current").stdout.strip() == f"worktree/{folder}"

    def test_different_routed_folders_use_distinct_worktree_branches(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        first_folder = "host__thread_conversation-conv_first"
        second_folder = "host__thread_conversation-conv_second"

        first = resolve_routed_host_worktree_cwd(
            first_folder,
            git_env["project"],
            [repo_ctx],
            recovered=False,
        )
        second = resolve_routed_host_worktree_cwd(
            second_folder,
            git_env["project"],
            [repo_ctx],
            recovered=False,
        )

        assert first.cwd != second.cwd
        assert _git(first.cwd, "branch", "--show-current").stdout.strip() == (
            f"worktree/{first_folder}"
        )
        assert _git(second.cwd, "branch", "--show-current").stdout.strip() == (
            f"worktree/{second_folder}"
        )

    def test_rejects_routed_slot_from_a_different_checkout(self, git_env: dict):
        folder = "host__thread_conversation-conv_wrong_checkout"
        worktree_path = git_env["worktrees_dir"] / folder
        _git(git_env["project"].parent, "init", "--initial-branch=main", str(worktree_path))
        (worktree_path / "foreign.txt").write_text("do not delete")

        with pytest.raises(RoutedHostWorktreeError, match="different checkout"):
            resolve_routed_host_worktree_cwd(
                folder,
                git_env["project"],
                [git_env["repo_ctx"]],
                recovered=False,
            )

        assert (worktree_path / "foreign.txt").read_text() == "do not delete"

    def test_rejects_routed_slot_on_another_branch(self, git_env: dict):
        folder = "host__thread_conversation-conv_wrong_branch"
        worktree_path = git_env["worktrees_dir"] / folder
        _git(
            git_env["project"],
            "worktree",
            "add",
            "-b",
            "worktree/unrelated",
            str(worktree_path),
            "origin/main",
        )

        with pytest.raises(RoutedHostWorktreeError, match="unexpected branch"):
            resolve_routed_host_worktree_cwd(
                folder,
                git_env["project"],
                [git_env["repo_ctx"]],
                recovered=False,
            )

        branch = _git(worktree_path, "branch", "--show-current").stdout.strip()
        assert branch == "worktree/unrelated"

    def test_rejects_routed_slot_symlinked_to_parent_checkout(self, git_env: dict):
        project = git_env["project"]
        folder = "host__thread_conversation-conv_symlink"
        worktree_path = git_env["worktrees_dir"] / folder
        _git(project, "checkout", "-b", f"worktree/{folder}")
        worktree_path.parent.mkdir(parents=True)
        worktree_path.symlink_to(project, target_is_directory=True)

        with pytest.raises(RoutedHostWorktreeError, match="symbolic link"):
            resolve_routed_host_worktree_cwd(
                folder,
                project,
                [git_env["repo_ctx"]],
                recovered=False,
            )

        assert worktree_path.is_symlink()

    def test_selects_source_repository_from_multiple_selected_repositories(self, git_env: dict):
        project = git_env["project"]
        other_root = project.parent / "other-project"
        _git(project.parent, "clone", str(git_env["origin"]), str(other_root))
        other_repo = RepoContext(
            slug="owner/other",
            root=other_root,
            worktrees_dir=git_env["worktrees_dir"] / "other",
        )
        folder = "host__thread_conversation-conv_multi_repo"

        result = resolve_routed_host_worktree_cwd(
            folder,
            project,
            [git_env["repo_ctx"], other_repo],
            recovered=False,
        )

        assert result.cwd == git_env["worktrees_dir"] / folder

    def test_dirty_legacy_source_blocks_recovered_routed_session(self, git_env: dict):
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]
        folder = "host__thread_conversation-conv_dirty"
        (project / "legacy.txt").write_text("uncommitted parent work")

        with pytest.raises(RoutedHostWorktreeError, match="uncommitted parent work"):
            resolve_routed_host_worktree_cwd(folder, project, [repo_ctx], recovered=True)

        assert not (git_env["worktrees_dir"] / folder).exists()

    def test_ahead_legacy_source_blocks_recovered_routed_session(self, git_env: dict):
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]
        folder = "host__thread_conversation-conv_ahead"
        (project / "legacy.txt").write_text("committed parent work")
        _git(project, "add", "legacy.txt")
        _git(project, "commit", "-m", "legacy parent commit")

        with pytest.raises(RoutedHostWorktreeError, match="ahead of main"):
            resolve_routed_host_worktree_cwd(folder, project, [repo_ctx], recovered=True)

        assert not (git_env["worktrees_dir"] / folder).exists()

    def test_missing_or_ambiguous_repository_source_fails_safely(self, git_env: dict):
        with pytest.raises(RoutedHostWorktreeError, match="exactly one"):
            resolve_routed_host_worktree_cwd(
                "host__thread_conversation-conv_invalid",
                git_env["project"],
                [],
                recovered=False,
            )
        with pytest.raises(RoutedHostWorktreeError, match="exactly one"):
            resolve_routed_host_worktree_cwd(
                "host__thread_conversation-conv_invalid",
                git_env["project"],
                [git_env["repo_ctx"], git_env["repo_ctx"]],
                recovered=False,
            )
