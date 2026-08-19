"""Tests for git worktree management.

Uses real git repos via tmp_path to validate actual git behavior.
"""

from __future__ import annotations

import fcntl
import os
import subprocess  # noqa: S404 - test helpers mock subprocess behavior and exceptions
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
    WorktreeError,
    WorktreeResult,
    ensure_worktree,
    install_repo_hooks,
    reconcile_worktrees_at_startup,
    resolve_routed_host_worktree_cwd,
    select_routed_host_repo,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test helper runs fixed git argv against temp repos
        ["git", *args],  # noqa: S607 - test helper deliberately resolves git from PATH
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_bare_origin(tmp_path: Path) -> Path:
    """Create a bare 'origin' repo with one commit on main."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")

    # Clone, commit, push to set up origin/main
    clone = tmp_path / "setup-clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@test.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "README.md").write_text("initial")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-m", "initial commit")
    _git(clone, "push", "origin", "main")
    return origin


def _make_project(tmp_path: Path, origin: Path) -> Path:
    """Clone origin into a 'project' directory (simulates PROJECT_ROOT)."""
    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))
    _git(project, "config", "user.email", "test@test.com")
    _git(project, "config", "user.name", "Test")
    return project


@pytest.mark.parametrize(
    ("config_name", "runner"),
    [
        ("prek.toml", "prek"),
    ],
)
def test_install_repo_hooks_uses_declared_runner(
    tmp_path: Path,
    config_name: str,
    runner: str,
) -> None:
    (tmp_path / config_name).write_text("")

    with patch("pynchy.host.git_ops.worktree.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        install_repo_hooks(tmp_path)

    run.assert_called_once_with(
        ["uv", "tool", "run", runner, "install"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_install_repo_hooks_prefers_prek(tmp_path: Path) -> None:
    (tmp_path / "prek.toml").write_text("")
    (tmp_path / ".pre-commit-config.yaml").write_text("")

    with patch("pynchy.host.git_ops.worktree.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        install_repo_hooks(tmp_path)

    assert run.call_args.args[0] == ["uv", "tool", "run", "prek", "install"]


def test_install_repo_hooks_skips_repositories_without_config(tmp_path: Path) -> None:
    with patch("pynchy.host.git_ops.worktree.subprocess.run") as run:
        install_repo_hooks(tmp_path)

    run.assert_not_called()


@pytest.fixture
def git_env(tmp_path: Path):
    """Set up origin + project repos with patched settings."""
    origin = _make_bare_origin(tmp_path)
    project = _make_project(tmp_path, origin)
    worktrees_dir = tmp_path / "worktrees"

    s = make_settings(project_root=project, worktrees_dir=worktrees_dir)
    repo_ctx = RepoContext(slug="owner/pynchy", root=project, worktrees_dir=worktrees_dir)

    with ExitStack() as stack:
        stack.enter_context(patch("pynchy.host.git_ops.utils._default_cwd", s.project_root))
        stack.enter_context(patch("pynchy.config.api.get_settings", return_value=s))
        # The filesystem origin exercises fetch/rebase mechanics; GitHub checkout
        # identity coverage lives with repository provisioning tests.
        stack.enter_context(patch("pynchy.host.git_ops.repo.ensure_repo_cloned", return_value=True))
        yield {
            "origin": origin,
            "project": project,
            "worktrees_dir": worktrees_dir,
            "repo_ctx": repo_ctx,
        }


# ---------------------------------------------------------------------------
# ensure_worktree tests
# ---------------------------------------------------------------------------


class TestEnsureWorktree:
    def test_creates_new_worktree(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        result = ensure_worktree("code-improver", repo_ctx)

        assert result.path == git_env["worktrees_dir"] / "code-improver"
        assert result.path.exists()
        assert (result.path / "README.md").read_text() == "initial"
        assert result.notices == []

        # Verify the branch was created
        branch_list = _git(git_env["project"], "branch", "--list", "worktree/code-improver")
        assert "worktree/code-improver" in branch_list.stdout

    def test_syncs_existing_worktree_with_notice(self, git_env: dict):
        """Pulling new commits produces a notice about auto-pulled changes."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        # Create worktree first
        result1 = ensure_worktree("code-improver", repo_ctx)
        wt_path = result1.path

        # Push a new commit to origin from the project
        (project / "new-file.txt").write_text("new content")
        _git(project, "add", "new-file.txt")
        _git(project, "commit", "-m", "add new file")
        _git(project, "push", "origin", "main")

        # Second call should merge latest origin/main and notify
        result2 = ensure_worktree("code-improver", repo_ctx)
        assert result2.path == wt_path
        assert (wt_path / "new-file.txt").read_text() == "new content"
        assert len(result2.notices) == 1
        assert "Auto-pulled remote changes" in result2.notices[0]

    def test_sync_merge_uses_scoped_git_identity(
        self,
        git_env: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]
        worktree_path = ensure_worktree("code-improver", repo_ctx).path

        (worktree_path / "local.txt").write_text("local")
        _git(worktree_path, "add", "local.txt")
        _git(worktree_path, "commit", "-m", "local change")
        (project / "remote.txt").write_text("remote")
        _git(project, "add", "remote.txt")
        _git(project, "commit", "-m", "remote change")
        _git(project, "push", "origin", "main")

        _git(project, "config", "--unset-all", "user.name")
        _git(project, "config", "--unset-all", "user.email")
        _git(project, "config", "user.useConfigOnly", "true")
        for variable in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        ):
            monkeypatch.delenv(variable, raising=False)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(project / "missing-global-config"))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

        result = ensure_worktree("code-improver", repo_ctx)

        assert all("merge of origin/main failed" not in notice for notice in result.notices)
        assert (worktree_path / "remote.txt").read_text() == "remote"
        merge_commit = _git(
            worktree_path, "rev-list", "--parents", "-n", "1", "HEAD"
        ).stdout.split()
        assert len(merge_commit) == 3
        identity = _git(
            worktree_path,
            "show",
            "-s",
            "--format=%an <%ae>|%cn <%ce>",
            "HEAD",
        ).stdout.strip()
        assert identity == "Pynchy <pynchy@localhost>|Pynchy <pynchy@localhost>"

    def test_no_notice_when_already_up_to_date(self, git_env: dict):
        """No notice when worktree is already current with origin."""
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("code-improver", repo_ctx)

        # Second call with no new commits
        result = ensure_worktree("code-improver", repo_ctx)
        assert result.notices == []

    def test_preserves_uncommitted_changes(self, git_env: dict):
        """Uncommitted changes survive sync and produce a notice."""
        repo_ctx = git_env["repo_ctx"]
        result1 = ensure_worktree("code-improver", repo_ctx)
        wt_path = result1.path

        # Leave uncommitted changes in the worktree
        (wt_path / "wip.txt").write_text("work in progress")

        result2 = ensure_worktree("code-improver", repo_ctx)
        assert result2.path == wt_path
        # WIP file is preserved
        assert (wt_path / "wip.txt").read_text() == "work in progress"
        # Notice about uncommitted changes
        assert len(result2.notices) == 1
        assert "uncommitted changes" in result2.notices[0]

    def test_fetch_failure_produces_notice(self, git_env: dict):
        """Failed fetch on existing worktree is a notice, not an error."""
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("code-improver", repo_ctx)

        # Break the remote so fetch fails
        _git(git_env["project"], "remote", "set-url", "origin", "/nonexistent/repo")

        result = ensure_worktree("code-improver", repo_ctx)
        assert result.path.exists()
        assert any("fetch failed" in n for n in result.notices)

    def test_error_propagates_for_new_worktree(self, git_env: dict):
        """WorktreeError raised when creating a new worktree with broken remote."""
        repo_ctx = git_env["repo_ctx"]
        _git(git_env["project"], "remote", "set-url", "origin", "/nonexistent/repo")

        with pytest.raises(WorktreeError, match="git fetch failed"):
            ensure_worktree("broken-group", repo_ctx)

    def test_broken_worktree_preserves_uncommitted_files(self, git_env: dict):
        """Corrupted Git metadata must not erase unfinished agent work."""
        repo_ctx = git_env["repo_ctx"]
        result1 = ensure_worktree("code-improver", repo_ctx)
        wt_path = result1.path

        (wt_path / "wip.txt").write_text("uncommitted work")
        git_file = wt_path / ".git"
        git_file.write_text("gitdir: /nonexistent/path/.git/worktrees/old-name\n")

        with pytest.raises(WorktreeError, match="recover it manually"):
            ensure_worktree("code-improver", repo_ctx)

        assert (wt_path / "wip.txt").read_text() == "uncommitted work"

    def test_reattaches_existing_branch_when_worktree_directory_is_missing(self, git_env: dict):
        """A removed child directory must not erase committed recovery work."""
        repo_ctx = git_env["repo_ctx"]
        worktree = ensure_worktree("host__thread_conversation-conv_recovery", repo_ctx).path
        (worktree / "recovered.txt").write_text("keep this commit")
        _git(worktree, "add", "recovered.txt")
        _git(worktree, "commit", "-m", "preserve routed recovery")
        _git(repo_ctx.root, "worktree", "remove", "--force", str(worktree))

        restored = ensure_worktree("host__thread_conversation-conv_recovery", repo_ctx)

        assert restored.path == worktree
        assert (restored.path / "recovered.txt").read_text() == "keep this commit"


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


# ---------------------------------------------------------------------------
# reconcile_worktrees_at_startup tests
# ---------------------------------------------------------------------------


class TestReconcileWorktreesAtStartup:
    def test_preserves_an_agent_rebase_already_in_progress(self, git_env: dict):
        """Startup must not abort conflict resolution owned by an agent."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]
        worktree = ensure_worktree("code-improver", repo_ctx).path

        (worktree / "README.md").write_text("agent change")
        _git(worktree, "add", "README.md")
        _git(worktree, "commit", "-m", "agent change")
        (project / "README.md").write_text("main change")
        _git(project, "add", "README.md")
        _git(project, "commit", "-m", "main change")

        with pytest.raises(subprocess.CalledProcessError):
            _git(worktree, "rebase", "main")
        (worktree / "README.md").write_text("agent conflict resolution")
        _git(worktree, "add", "README.md")

        with patch("pynchy.host.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": []})

        assert (worktree / "README.md").read_text() == "agent conflict resolution"
        assert "agent conflict resolution" in _git(worktree, "diff", "--cached").stdout

    def test_rebases_diverged_worktree(self, git_env: dict):
        """Diverged worktree branch is rebased onto main at startup."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        # Create worktree and commit
        result = ensure_worktree("code-improver", repo_ctx)
        wt_path = result.path
        (wt_path / "feature.txt").write_text("worktree work")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "worktree commit")

        # Advance main to create divergence
        (project / "other.txt").write_text("main work")
        _git(project, "add", "other.txt")
        _git(project, "commit", "-m", "advance main")

        # Verify divergence exists
        ahead = _git(project, "rev-list", "main..worktree/code-improver", "--count")
        behind = _git(project, "rev-list", "worktree/code-improver..main", "--count")
        assert int(ahead.stdout.strip()) > 0
        assert int(behind.stdout.strip()) > 0

        with patch("pynchy.host.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": []})

        # After reconcile, worktree branch should be ahead of main (rebased), not diverged
        behind_after = _git(project, "rev-list", "worktree/code-improver..main", "--count")
        assert int(behind_after.stdout.strip()) == 0

    def test_skips_non_diverged_worktree(self, git_env: dict):
        """Worktrees that aren't diverged are left alone."""
        repo_ctx = git_env["repo_ctx"]
        result = ensure_worktree("code-improver", repo_ctx)
        wt_path = result.path

        # Commit in worktree (ahead only, not diverged)
        (wt_path / "feature.txt").write_text("feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "feature")

        head_before = _git(wt_path, "rev-parse", "HEAD").stdout.strip()

        with patch("pynchy.host.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": []})

        # HEAD unchanged — no rebase needed
        head_after = _git(wt_path, "rev-parse", "HEAD").stdout.strip()
        assert head_before == head_after

    def test_handles_no_worktrees_dir(self, git_env: dict):
        """Runs cleanly when worktrees dir doesn't exist."""
        repo_ctx = git_env["repo_ctx"]
        # worktrees_dir doesn't exist yet — should not raise
        with patch("pynchy.host.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": []})

    def test_creates_missing_worktrees_at_startup(self, git_env: dict):
        """Worktrees for repo_access folders are created if missing."""
        repo_ctx = git_env["repo_ctx"]
        with patch("pynchy.host.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(
                repo_groups={"owner/pynchy": ["admin-1", "code-improver"]}
            )

        worktrees_dir = git_env["worktrees_dir"]
        assert (worktrees_dir / "admin-1").exists()
        assert (worktrees_dir / "code-improver").exists()

        # Both should be valid git repos
        _git(worktrees_dir / "admin-1", "status")
        _git(worktrees_dir / "code-improver", "status")

    def test_idempotent(self, git_env: dict):
        """Calling twice with same folders doesn't break anything."""
        repo_ctx = git_env["repo_ctx"]
        folders = ["admin-1", "code-improver"]
        with patch("pynchy.host.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": folders})

        # Record state
        worktrees_dir = git_env["worktrees_dir"]
        head_admin = _git(worktrees_dir / "admin-1", "rev-parse", "HEAD").stdout.strip()
        head_ci = _git(worktrees_dir / "code-improver", "rev-parse", "HEAD").stdout.strip()

        # Second call — should be a no-op
        with patch("pynchy.host.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": folders})

        assert _git(worktrees_dir / "admin-1", "rev-parse", "HEAD").stdout.strip() == head_admin
        assert _git(worktrees_dir / "code-improver", "rev-parse", "HEAD").stdout.strip() == head_ci
